import logging
from collections.abc import Callable

import requests

from src.gmail.client import EmailMessage
from src.triage.engine import TriageResult

logger = logging.getLogger(__name__)


def _describe_send_error(exc: requests.RequestException) -> str:
    # str(exc) embeds the request URL, which carries the Telegram bot token - never log it.
    # The response body (e.g. Telegram's {"description": "..."}) has no secret in it and is
    # exactly what's needed to diagnose a 400, so keep that, truncated defensively.
    response = getattr(exc, "response", None)
    if response is None:
        return type(exc).__name__
    detail = (response.text or "").strip()[:300]
    return f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"


class AlertGateway:
    def __init__(self, telegram_bot_token: str = "", telegram_chat_id: str = "", discord_webhook_url: str = "") -> None:
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = discord_webhook_url

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
        response = requests.get(self.discord_webhook_url, timeout=10)
        response.raise_for_status()
        return "webhook reachable"

    def send_urgent_alert(self, email: EmailMessage, triage: TriageResult, summary: str = "") -> None:
        text = (
            f"🚨 Urgent email detected\nFrom: {email.sender}\nSubject: {email.subject}"
            f"\nCategory: {triage.category}\nConfidence: {triage.confidence:.2f}\n\n{summary}"
        )
        self._safe_send("telegram", self._send_telegram, text)
        self._safe_send("discord", self._send_discord, text)

    def send_telegram_text(self, text: str) -> None:
        self._safe_send("telegram", self._send_telegram, text)

    @staticmethod
    def _safe_send(channel: str, send: Callable[[str], None], message: str) -> None:
        # A failed notification must never crash email processing or startup; it must also
        # never disappear silently, or a dead integration goes unnoticed indefinitely.
        try:
            send(message)
        except requests.RequestException as exc:
            logger.warning("Failed to send %s notification: %s", channel, _describe_send_error(exc))

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
        if not self.discord_webhook_url:
            return
        response = requests.post(self.discord_webhook_url, json={"content": message}, timeout=10)
        response.raise_for_status()
