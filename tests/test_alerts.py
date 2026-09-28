import unittest
from unittest.mock import patch

import requests

from src.gateways.alerts import AlertGateway, format_urgent_alert
from src.gmail.client import EmailMessage
from src.triage.engine import TriageResult


def make_email(**overrides) -> EmailMessage:
    fields = {
        "id": "1",
        "thread_id": "t",
        "sender": "notifications@github.com",
        "subject": "Run failed: Release - prod",
        "snippet": "s",
        "body": "b",
    }
    return EmailMessage(**{**fields, **overrides})


class FormatUrgentAlertTests(unittest.TestCase):
    def test_layout_has_labeled_header_then_summary(self):
        text = format_urgent_alert(make_email(), TriageResult("high", "notification_systeme", 0.34), "• point")

        self.assertEqual(
            text,
            "🚨 Email urgent\nDe : notifications@github.com\nObjet : Run failed: Release - prod\n"
            "Catégorie : notification_systeme · confiance 34%\n\n• point",
        )

    def test_summary_markdown_is_stripped_for_plain_text_delivery(self):
        text = format_urgent_alert(make_email(), TriageResult("high", "personnel", 0.9), "- **Event:** failed")

        self.assertIn("• Event: failed", text)
        self.assertNotIn("**", text)

    def test_empty_summary_leaves_no_trailing_blank_section(self):
        text = format_urgent_alert(make_email(), TriageResult("high", "personnel", 0.9), "")

        self.assertTrue(text.endswith("confiance 90%"))

    def test_message_is_truncated_under_the_telegram_limit(self):
        text = format_urgent_alert(make_email(), TriageResult("high", "personnel", 0.9), "x" * 10_000)

        self.assertLessEqual(len(text), 4000)


class SafeSendTests(unittest.TestCase):
    def test_delivery_failure_is_logged_and_swallowed(self):
        gateway = AlertGateway(discord_webhook_url="https://discord.com/api/webhooks/1/abc")
        with (
            patch("src.gateways.alerts.requests.post", side_effect=requests.ConnectionError("down")),
            self.assertLogs("src.gateways.alerts", level="WARNING"),
        ):
            gateway.send_urgent_alert(make_email(), TriageResult("high", "personnel", 0.9), "s")


if __name__ == "__main__":
    unittest.main()
