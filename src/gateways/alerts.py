import logging
import re

from src.domain import Button, EmailMessage, TriageResult
from src.expiring_set import ExpiringSet
from src.formatting import strip_markdown, truncate
from src.gateways.circuit_breaker import CircuitBreaker
from src.interactions.callbacks import feedback_buttons
from src.observability.metrics import Metrics
from src.ports import AlertChannel, ChannelDeliveryError
from src.triage.rules import is_automated_sender

logger = logging.getLogger(__name__)

ALERT_MESSAGE_LIMIT = 4000
DEDUP_WINDOW_SECONDS = 30 * 60
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
    return truncate(text, ALERT_MESSAGE_LIMIT)


class AlertDeliveryError(RuntimeError):
    pass


class AlertGateway:
    def __init__(self, channels: list[AlertChannel], feedback_buttons: bool = False) -> None:
        self.channels = list(channels)
        self._feedback_buttons = feedback_buttons
        self._breakers = {channel.name: CircuitBreaker() for channel in self.channels}
        self._recent_automated_alerts = ExpiringSet(DEDUP_WINDOW_SECONDS)

    def send_urgent_alert(
        self, email: EmailMessage, triage: TriageResult, summary: str = ""
    ) -> int | None:
        """Returns the message id from the first interactive channel that delivered it.

        Raises AlertDeliveryError when every configured channel failed, so the mail is retried.
        """
        if self._is_duplicate_automated_alert(email):
            logger.info("Skipping duplicate alert for %s: %s", email.sender, email.subject)
            Metrics.mark_alert("all", "deduplicated")
            return None
        text = format_urgent_alert(email, triage, summary)
        buttons = feedback_buttons(email.id) if self._feedback_buttons else None
        outcomes = []
        chat_message_id = None
        for channel in self._configured():
            delivered, message_id = self._safe_send(
                channel, text, buttons if channel.interactive else None
            )
            outcomes.append(delivered)
            if chat_message_id is None and delivered and channel.interactive:
                chat_message_id = message_id
        if outcomes and not any(outcomes):
            raise AlertDeliveryError(f"No channel delivered the alert for {email.subject!r}")
        if outcomes:
            self._record_automated_alert(email)
        return chat_message_id

    def send_text(self, text: str) -> None:
        """Best-effort message to the interactive channels only."""
        for channel in self._configured():
            if channel.interactive:
                self._safe_send(channel, text, None)

    def _configured(self) -> list[AlertChannel]:
        return [channel for channel in self.channels if channel.is_configured]

    def _is_duplicate_automated_alert(self, email: EmailMessage) -> bool:
        # Repeated CI failures share sender and subject; human mail is never deduplicated.
        return is_automated_sender(email.sender) and dedup_key(email) in self._recent_automated_alerts

    def _record_automated_alert(self, email: EmailMessage) -> None:
        if is_automated_sender(email.sender):
            self._recent_automated_alerts.add(dedup_key(email))

    def _safe_send(
        self, channel: AlertChannel, text: str, buttons: list[list[Button]] | None
    ) -> tuple[bool, int | None]:
        # A failed notification must never crash email processing or startup; it must also
        # never disappear silently, or a dead integration goes unnoticed indefinitely.
        breaker = self._breakers[channel.name]
        if not breaker.allow():
            Metrics.mark_alert(channel.name, "skipped")
            return False, None
        try:
            message_id = channel.send(text, buttons)
        except ChannelDeliveryError as exc:
            Metrics.mark_alert(channel.name, "failed")
            logger.warning("Failed to send %s notification: %s", channel.name, exc)
            if breaker.record_failure():
                logger.error(
                    "%s notifications failing repeatedly; pausing them for a while", channel.name
                )
            return False, None
        breaker.record_success()
        Metrics.mark_alert(channel.name, "sent")
        return True, message_id
