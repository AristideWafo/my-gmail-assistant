import requests

from src.gmail.client import EmailMessage
from src.triage.engine import TriageResult


class AlertGateway:
    def __init__(self, telegram_bot_token: str = "", telegram_chat_id: str = "", discord_webhook_url: str = "") -> None:
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = discord_webhook_url

    def send_urgent_alert(self, email: EmailMessage, triage: TriageResult, summary: str = "") -> None:
        text = (
            f"🚨 Urgent email detected\nFrom: {email.sender}\nSubject: {email.subject}"
            f"\nCategory: {triage.category}\nConfidence: {triage.confidence:.2f}\n\n{summary}"
        )
        self._send_telegram(text)
        self._send_discord(text)

    def _send_telegram(self, message: str) -> None:
        if not (self.telegram_bot_token and self.telegram_chat_id):
            return
        requests.post(
            f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage",
            json={"chat_id": self.telegram_chat_id, "text": message},
            timeout=10,
        )

    def _send_discord(self, message: str) -> None:
        if not self.discord_webhook_url:
            return
        requests.post(self.discord_webhook_url, json={"content": message}, timeout=10)
