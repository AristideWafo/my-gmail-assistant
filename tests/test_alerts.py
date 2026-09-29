import unittest
from unittest.mock import MagicMock, patch

import requests

from src.domain import EmailMessage, TriageResult
from src.gateways.alerts import AlertDeliveryError, AlertGateway, dedup_key, format_urgent_alert
from src.gateways.telegram_bot import TelegramApiError
from src.health import StartupCheckMode, run_startup_checks


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


def make_bot(message_id=77, error=None) -> MagicMock:
    bot = MagicMock(configured=True)
    bot.send_message.return_value = message_id
    if error is not None:
        bot.send_message.side_effect = error
    return bot


def telegram_down() -> TelegramApiError:
    return TelegramApiError("Telegram sendMessage failed: ConnectionError (status n/a)")


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
    def test_delivery_failure_is_logged_and_reported_to_the_caller_for_retry(self):
        gateway = AlertGateway(discord_webhook_url="https://discord.com/api/webhooks/1/abc")
        with (
            patch("src.gateways.alerts.requests.post", side_effect=requests.ConnectionError("down")),
            self.assertLogs("src.gateways.alerts", level="WARNING"),
            self.assertRaises(AlertDeliveryError),
        ):
            gateway.send_urgent_alert(make_email(), TriageResult("high", "personnel", 0.9), "s")

    def test_one_delivering_channel_is_enough(self):
        gateway = AlertGateway(
            telegram=make_bot(error=telegram_down()), discord_webhook_url=VALID_DISCORD_URL
        )

        with patch("src.gateways.alerts.requests.post"), self.assertLogs("src.gateways.alerts"):
            self.assertIsNone(gateway.send_urgent_alert(make_email(), HIGH, "s"))

    def test_discord_failure_log_never_contains_the_webhook_token(self):
        gateway = AlertGateway(discord_webhook_url=VALID_DISCORD_URL)
        error = requests.ConnectionError(f"Max retries exceeded with url: {VALID_DISCORD_URL}")
        with (
            patch("src.gateways.alerts.requests.post", side_effect=error),
            self.assertLogs("src.gateways.alerts", level="WARNING") as logs,
            self.assertRaises(AlertDeliveryError),
        ):
            gateway.send_urgent_alert(make_email(), HIGH, "s")

        self.assertNotIn("abc-DEF_1", "\n".join(logs.output))
        self.assertIn("ConnectionError", logs.output[0])

    def test_unconfigured_bot_is_not_a_channel(self):
        bot = make_bot()
        bot.configured = False

        self.assertIsNone(AlertGateway(telegram=bot).send_urgent_alert(make_email(), HIGH, "s"))
        bot.send_message.assert_not_called()

    def test_no_configured_channel_is_not_a_failure(self):
        AlertGateway().send_urgent_alert(make_email(), HIGH, "s")

    def test_alert_skipped_by_open_breakers_is_reported_as_undelivered(self):
        bot = make_bot(error=telegram_down())
        gateway = AlertGateway(telegram=bot)
        with self.assertLogs("src.gateways.alerts"):
            for i in range(5):
                with self.assertRaises(AlertDeliveryError):
                    gateway.send_urgent_alert(make_email(sender=f"p{i}@x.io"), HIGH, "s")
        bot.send_message.reset_mock()
        with self.assertRaises(AlertDeliveryError):
            gateway.send_urgent_alert(make_email(sender="p9@x.io"), HIGH, "s")
        bot.send_message.assert_not_called()

    def test_failed_alert_does_not_poison_the_dedup_window(self):
        bot = make_bot(error=telegram_down())
        gateway = AlertGateway(telegram=bot)
        email = make_email()
        with self.assertLogs("src.gateways.alerts"), self.assertRaises(AlertDeliveryError):
            gateway.send_urgent_alert(email, HIGH, "s")

        bot.send_message.side_effect = None
        self.assertEqual(gateway.send_urgent_alert(email, HIGH, "s"), 77)
        self.assertEqual(bot.send_message.call_count, 2)

    def test_discord_content_is_truncated_to_its_2000_character_limit(self):
        gateway = AlertGateway(discord_webhook_url=VALID_DISCORD_URL)
        with patch("src.gateways.alerts.requests.post") as post:
            gateway.send_urgent_alert(make_email(), HIGH, "x" * 3500)

        self.assertLessEqual(len(post.call_args.kwargs["json"]["content"]), 2000)


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
        bot = make_bot()
        gateway = AlertGateway(telegram=bot)
        gateway.send_urgent_alert(make_email(subject="Run failed (53fc758)"), HIGH, "s")
        second = gateway.send_urgent_alert(make_email(subject="Run failed (acc1a3a)"), HIGH, "s")

        self.assertIsNone(second)
        self.assertEqual(bot.send_message.call_count, 1)

    def test_human_mail_is_never_deduplicated(self):
        bot = make_bot()
        gateway = AlertGateway(telegram=bot)
        email = make_email(sender="celine@outlook.com", subject="Rencontre demain")
        gateway.send_urgent_alert(email, HIGH, "s")
        gateway.send_urgent_alert(email, HIGH, "s")

        self.assertEqual(bot.send_message.call_count, 2)


class CircuitBreakerIntegrationTests(unittest.TestCase):
    def test_channel_pauses_after_repeated_failures_without_affecting_the_other(self):
        bot = make_bot()
        gateway = AlertGateway(telegram=bot, discord_webhook_url=VALID_DISCORD_URL)

        with patch(
            "src.gateways.alerts.requests.post", side_effect=requests.ConnectionError("down")
        ) as discord_post, self.assertLogs("src.gateways.alerts", level="WARNING") as logs:
            for i in range(8):
                gateway.send_urgent_alert(make_email(sender=f"p{i}@x.io", subject=f"s{i}"), HIGH, "s")

        self.assertEqual(discord_post.call_count, 5)
        self.assertEqual(bot.send_message.call_count, 8)
        self.assertEqual(sum("pausing" in line for line in logs.output), 1)


class TelegramAlertTests(unittest.TestCase):
    def test_returns_the_telegram_message_id(self):
        gateway = AlertGateway(telegram=make_bot(message_id=321))

        self.assertEqual(gateway.send_urgent_alert(make_email(), HIGH, "s"), 321)

    def test_feedback_buttons_carry_the_gmail_id(self):
        bot = make_bot()
        AlertGateway(telegram=bot, feedback_buttons=True).send_urgent_alert(
            make_email(id="18c2f0a1b2c3d4e5"), HIGH, "s"
        )

        self.assertEqual(
            bot.send_message.call_args.kwargs["buttons"],
            [
                [
                    ("Valider", "fb:v:18c2f0a1b2c3d4e5"),
                    ("Faux-Urgent", "fb:u:18c2f0a1b2c3d4e5"),
                    ("Faux-Spam", "fb:s:18c2f0a1b2c3d4e5"),
                ]
            ],
        )

    def test_no_buttons_without_the_flag(self):
        bot = make_bot()
        AlertGateway(telegram=bot).send_urgent_alert(make_email(), HIGH, "s")

        self.assertIsNone(bot.send_message.call_args.kwargs["buttons"])

    def test_oversized_id_sends_the_alert_without_buttons(self):
        bot = make_bot()
        AlertGateway(telegram=bot, feedback_buttons=True).send_urgent_alert(
            make_email(id="x" * 80), HIGH, "s"
        )

        bot.send_message.assert_called_once()
        self.assertIsNone(bot.send_message.call_args.kwargs["buttons"])

    def test_check_telegram_reports_the_bot_username(self):
        bot = make_bot()
        bot.get_me.return_value = {"username": "mybot"}

        self.assertEqual(AlertGateway(telegram=bot).check_telegram(), "bot @mybot reachable")


if __name__ == "__main__":
    unittest.main()
