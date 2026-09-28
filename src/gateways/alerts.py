import logging
import re
from collections.abc import Callable

import requests

from src.errors import ConfigurationError
from src.expiring_set import ExpiringSet
from src.formatting import strip_markdown, truncate
from src.gateways.circuit_breaker import CircuitBreaker
from src.gmail.client import EmailMessage
from src.observability.metrics import Metrics
from src.triage.engine import TriageResult
from src.triage.rules import is_automated_sender

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4000
DISCORD_MESSAGE_LIMIT = 2000
DISCORD_URL_HINT = "Expected https://discord.com/api/webhooks/<id>/<token>; recreate the webhook if the token is lost."
DEDUP_WINDOW_SECONDS = 30 * 60
DISCORD_WEBHOOK_RE = re.compile(r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$")
_COMMIT_SHA_RE = re.compile(r"\s*\([0-9a-f]{7,40}\)")
_NUMBER_REF_RE = re.compile(r"#\d+")


def dedup_key(email: EmailMessage) -> tuple[str, str]:
    subject = _NUMBER_REF_RE.sub("", _COMMIT_SHA_RE.sub("", email.subject))
    return email.sender.lower(), " ".join(subject.lower().split())


def format_urgent_alert(email: EmailMessage, triage: TriageResult, summary: str = "") -> str:
    lines = [
        "🚨 Email urgent",
        f"De : {email.sender}",
        f"Objet : {email.subject}",
        f"Catégorie : {triage.category} · confiance {triage.confidence:.0%}",
    ]
    text = "\n".join(lines)
    body = strip_markdown(summary)
    if body:
        text = f"{text}\n\n{body}"
    return truncate(text, TELEGRAM_MESSAGE_LIMIT)


class AlertDeliveryError(RuntimeError):
    pass


class AlertGateway:
    def __init__(self, telegram_bot_token: str = "", telegram_chat_id: str = "", discord_webhook_url: str = "") -> None:
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = discord_webhook_url
        self._discord_url_valid = bool(DISCORD_WEBHOOK_RE.match(discord_webhook_url))
        self._breakers = {"telegram": CircuitBreaker(), "discord": CircuitBreaker()}
        self._recent_automated_alerts = ExpiringSet(DEDUP_WINDOW_SECONDS)
        if discord_webhook_url and not self._discord_url_valid:
            logger.error("DISCORD_WEBHOOK_URL is malformed; Discord alerts are disabled. %s", DISCORD_URL_HINT)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_webhook_url)

    def check_telegram(self) -> str:
        response = requests.get(f"https://api.telegram.org/bot{self.telegram_bot_token}/getMe", timeout=10)
        response.raise_for_status()
        return f"bot @{response.json()['result']['username']} reachable"

    def check_discord(self) -> str:
        if not self._discord_url_valid:
            raise ConfigurationError(f"webhook URL malformed. {DISCORD_URL_HINT}")
        response = requests.get(self.discord_webhook_url, timeout=10)
        response.raise_for_status()
        return "webhook reachable"

    def send_urgent_alert(self, email: EmailMessage, triage: TriageResult, summary: str = "") -> None:
        """Raises AlertDeliveryError when every configured channel failed, so the mail is retried."""
        if self._is_duplicate_automated_alert(email):
            logger.info("Skipping duplicate alert for %s: %s", email.sender, email.subject)
            Metrics.mark_alert("all", "deduplicated")
            return
        text = format_urgent_alert(email, triage, summary)
        outcomes = []
        if self.telegram_configured:
            outcomes.append(self._safe_send("telegram", self._send_telegram, text))
        if self._discord_url_valid:
            outcomes.append(self._safe_send("discord", self._send_discord, text))
        if outcomes and not any(outcomes):
            raise AlertDeliveryError(f"No channel delivered the alert for {email.subject!r}")
        if outcomes:
            self._record_automated_alert(email)

    def send_telegram_text(self, text: str) -> None:
        self._safe_send("telegram", self._send_telegram, text)

    def _is_duplicate_automated_alert(self, email: EmailMessage) -> bool:
        # Repeated CI failures share sender and subject; human mail is never deduplicated.
        return is_automated_sender(email.sender) and dedup_key(email) in self._recent_automated_alerts

    def _record_automated_alert(self, email: EmailMessage) -> None:
        if is_automated_sender(email.sender):
            self._recent_automated_alerts.add(dedup_key(email))

    def _safe_send(self, channel: str, send: Callable[[str], None], message: str) -> bool:
        # A failed notification must never crash email processing or startup; it must also
        # never disappear silently, or a dead integration goes unnoticed indefinitely.
        breaker = self._breakers[channel]
        if not breaker.allow():
            Metrics.mark_alert(channel, "skipped")
            return False
        try:
            send(message)
        except requests.RequestException as exc:
            Metrics.mark_alert(channel, "failed")
            logger.warning("Failed to send %s notification: %s", channel, exc)
            if breaker.record_failure():
                logger.error("%s notifications failing repeatedly; pausing them for a while", channel)
            return False
        breaker.record_success()
        Metrics.mark_alert(channel, "sent")
        return True

    def _send_telegram(self, message: str) -> None:
        if not (self.telegram_bot_token and self.telegram_chat_id):
            return
        response = requests.post(
            f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage",
            json={"chat_id": self.telegram_chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()

    def _send_discord(self, message: str) -> None:
        if not self._discord_url_valid:
            return
        response = requests.post(self.discord_webhook_url, json={"content": truncate(message, DISCORD_MESSAGE_LIMIT)}, timeout=10)
        response.raise_for_status()
