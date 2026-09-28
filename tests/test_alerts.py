import unittest
from unittest.mock import MagicMock, patch

import requests

from src.gateways.alerts import AlertGateway, dedup_key, format_urgent_alert
from src.gmail.client import EmailMessage
from src.health import StartupCheckMode, run_startup_checks
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


VALID_DISCORD_URL = "https://discord.com/api/webhooks/123/abc-DEF_1"
HIGH = TriageResult("high", "alerte_technique", 0.9)


class DiscordWebhookValidationTests(unittest.TestCase):
    def test_url_without_token_is_rejected_at_startup_without_leaking_it(self):
        gateway = AlertGateway(discord_webhook_url="https://discord.com/api/webhooks/123456789")

        results = run_startup_checks({"discord": gateway.check_discord}, StartupCheckMode.WARN)

        self.assertEqual(results[0].status, "failed")
        self.assertIn("malformed", results[0].detail)
        self.assertNotIn("123456789", results[0].detail)

    def test_malformed_url_is_logged_once_and_never_posted_to(self):
        with self.assertLogs("src.gateways.alerts", level="ERROR"):
            gateway = AlertGateway(discord_webhook_url="https://discord.com/api/webhooks/123")

        with patch("src.gateways.alerts.requests.post") as post:
            gateway.send_urgent_alert(make_email(), HIGH, "s")

        post.assert_not_called()

    def test_valid_url_forms_are_accepted(self):
        for url in (VALID_DISCORD_URL, "https://discordapp.com/api/webhooks/1/tok", "https://canary.discord.com/api/webhooks/1/tok/"):
            with self.subTest(url=url), patch("src.gateways.alerts.requests.get"):
                self.assertEqual(AlertGateway(discord_webhook_url=url).check_discord(), "webhook reachable")


class DedupTests(unittest.TestCase):
    def test_key_ignores_commit_sha_and_numbers(self):
        first = make_email(subject="Run failed: Release - prod (53fc758)")
        second = make_email(subject="Run failed: Release - prod (acc1a3a)")
        self.assertEqual(dedup_key(first), dedup_key(second))

    def test_key_differs_per_sender_and_subject(self):
        base = make_email()
        self.assertNotEqual(dedup_key(base), dedup_key(make_email(subject="Run failed: Docker")))
        self.assertNotEqual(dedup_key(base), dedup_key(make_email(sender="other@github.com")))

    def test_repeated_automated_alert_is_sent_once(self):
        gateway = AlertGateway(telegram_bot_token="t", telegram_chat_id="1")
        with patch("src.gateways.alerts.requests.post") as post:
            gateway.send_urgent_alert(make_email(subject="Run failed (53fc758)"), HIGH, "s")
            gateway.send_urgent_alert(make_email(subject="Run failed (acc1a3a)"), HIGH, "s")

        self.assertEqual(post.call_count, 1)

    def test_human_mail_is_never_deduplicated(self):
        gateway = AlertGateway(telegram_bot_token="t", telegram_chat_id="1")
        email = make_email(sender="celine@outlook.com", subject="Rencontre demain")
        with patch("src.gateways.alerts.requests.post") as post:
            gateway.send_urgent_alert(email, HIGH, "s")
            gateway.send_urgent_alert(email, HIGH, "s")

        self.assertEqual(post.call_count, 2)


class CircuitBreakerIntegrationTests(unittest.TestCase):
    def test_channel_pauses_after_repeated_failures_without_affecting_the_other(self):
        gateway = AlertGateway(telegram_bot_token="t", telegram_chat_id="1", discord_webhook_url=VALID_DISCORD_URL)
        telegram_ok = MagicMock()

        def post(url, **kwargs):
            if "discord" in url:
                raise requests.ConnectionError("down")
            return telegram_ok

        with patch("src.gateways.alerts.requests.post", side_effect=post) as mock_post, self.assertLogs(
            "src.gateways.alerts", level="WARNING"
        ) as logs:
            for i in range(8):
                gateway.send_urgent_alert(make_email(sender=f"p{i}@x.io", subject=f"s{i}"), HIGH, "s")

        discord_calls = [c for c in mock_post.call_args_list if "discord" in c.args[0]]
        telegram_calls = [c for c in mock_post.call_args_list if "telegram" in c.args[0]]
        self.assertEqual(len(discord_calls), 5)
        self.assertEqual(len(telegram_calls), 8)
        self.assertEqual(sum("pausing" in line for line in logs.output), 1)


if __name__ == "__main__":
    unittest.main()
