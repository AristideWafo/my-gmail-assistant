import unittest

from prometheus_client import REGISTRY

from src.domain import EmailMessage, TriageResult
from src.gateways.alerts import AlertDeliveryError, AlertGateway, dedup_key, format_urgent_alert
from src.ports import ChannelDeliveryError


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


class FakeChannel:
    def __init__(self, name="chat", interactive=True, configured=True, message_id=77, error=None):
        self.name = name
        self.interactive = interactive
        self.is_configured = configured
        self.message_id = message_id if interactive else None
        self.error = error
        self.sent: list[tuple[str, object]] = []

    def check_connection(self) -> str:
        return "ok"

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        if self.error is not None:
            raise self.error
        return self.message_id


def down() -> ChannelDeliveryError:
    return ChannelDeliveryError("ConnectionError (status n/a)")


def sent_count() -> float:
    return REGISTRY.get_sample_value(
        "alerts_total", {"channel": "webhook", "status": "sent"}
    ) or 0.0


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


HIGH = TriageResult("high", "alerte_technique", 0.9)


class DeliveryTests(unittest.TestCase):
    def test_all_channels_failing_is_logged_and_reported_to_the_caller_for_retry(self):
        gateway = AlertGateway(
            [FakeChannel(error=down()), FakeChannel("webhook", False, error=down())]
        )

        with (
            self.assertLogs("src.gateways.alerts", level="WARNING") as logs,
            self.assertRaises(AlertDeliveryError),
        ):
            gateway.send_urgent_alert(make_email(), HIGH, "s")

        self.assertIn("ConnectionError (status n/a)", logs.output[0])

    def test_one_delivering_channel_is_enough(self):
        gateway = AlertGateway([FakeChannel(error=down()), FakeChannel("webhook", False)])

        with self.assertLogs("src.gateways.alerts"):
            self.assertIsNone(gateway.send_urgent_alert(make_email(), HIGH, "s"))

    def test_returns_the_first_delivered_interactive_message_id(self):
        channels = [
            FakeChannel("webhook", False),
            FakeChannel("a", error=down()),
            FakeChannel("b", message_id=321),
            FakeChannel("c", message_id=999),
        ]

        with self.assertLogs("src.gateways.alerts"):
            message_id = AlertGateway(channels).send_urgent_alert(make_email(), HIGH, "s")

        self.assertEqual(message_id, 321)
        self.assertTrue(all(len(channel.sent) == 1 for channel in channels))

    def test_unconfigured_channel_is_skipped(self):
        channel = FakeChannel(configured=False)

        self.assertIsNone(AlertGateway([channel]).send_urgent_alert(make_email(), HIGH, "s"))
        self.assertEqual(channel.sent, [])

    def test_no_configured_channel_is_not_a_failure(self):
        AlertGateway([]).send_urgent_alert(make_email(), HIGH, "s")
        AlertGateway([FakeChannel(configured=False)]).send_urgent_alert(make_email(), HIGH, "s")

    def test_unexpected_errors_are_not_swallowed(self):
        gateway = AlertGateway([FakeChannel(error=ValueError("bug"))])

        with self.assertRaises(ValueError):
            gateway.send_urgent_alert(make_email(), HIGH, "s")

    def test_successful_delivery_is_counted_per_channel(self):
        before = sent_count()

        AlertGateway([FakeChannel("webhook", False)]).send_urgent_alert(make_email(), HIGH, "s")

        self.assertEqual(sent_count(), before + 1)

    def test_alert_skipped_by_open_breakers_is_reported_as_undelivered(self):
        channel = FakeChannel(error=down())
        gateway = AlertGateway([channel])
        with self.assertLogs("src.gateways.alerts"):
            for i in range(5):
                with self.assertRaises(AlertDeliveryError):
                    gateway.send_urgent_alert(make_email(sender=f"p{i}@x.io"), HIGH, "s")
        channel.sent.clear()
        with self.assertRaises(AlertDeliveryError):
            gateway.send_urgent_alert(make_email(sender="p9@x.io"), HIGH, "s")
        self.assertEqual(channel.sent, [])

    def test_failed_alert_does_not_poison_the_dedup_window(self):
        channel = FakeChannel(error=down())
        gateway = AlertGateway([channel])
        email = make_email()
        with self.assertLogs("src.gateways.alerts"), self.assertRaises(AlertDeliveryError):
            gateway.send_urgent_alert(email, HIGH, "s")

        channel.error = None
        self.assertEqual(gateway.send_urgent_alert(email, HIGH, "s"), 77)
        self.assertEqual(len(channel.sent), 2)


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
        channel = FakeChannel()
        gateway = AlertGateway([channel])
        gateway.send_urgent_alert(make_email(subject="Run failed (53fc758)"), HIGH, "s")
        second = gateway.send_urgent_alert(make_email(subject="Run failed (acc1a3a)"), HIGH, "s")

        self.assertIsNone(second)
        self.assertEqual(len(channel.sent), 1)

    def test_human_mail_is_never_deduplicated(self):
        channel = FakeChannel()
        gateway = AlertGateway([channel])
        email = make_email(sender="celine@outlook.com", subject="Rencontre demain")
        gateway.send_urgent_alert(email, HIGH, "s")
        gateway.send_urgent_alert(email, HIGH, "s")

        self.assertEqual(len(channel.sent), 2)


class CircuitBreakerIntegrationTests(unittest.TestCase):
    def test_channel_pauses_after_repeated_failures_without_affecting_the_other(self):
        healthy, failing = FakeChannel(), FakeChannel("webhook", False, error=down())
        gateway = AlertGateway([healthy, failing])

        with self.assertLogs("src.gateways.alerts", level="WARNING") as logs:
            for i in range(8):
                gateway.send_urgent_alert(make_email(sender=f"p{i}@x.io", subject=f"s{i}"), HIGH, "s")

        self.assertEqual(len(failing.sent), 5)
        self.assertEqual(len(healthy.sent), 8)
        self.assertEqual(sum("pausing" in line for line in logs.output), 1)


class ButtonTests(unittest.TestCase):
    def test_feedback_buttons_carry_the_gmail_id_on_interactive_channels_only(self):
        chat, webhook = FakeChannel(), FakeChannel("webhook", False)
        AlertGateway([chat, webhook], feedback_buttons=True).send_urgent_alert(
            make_email(id="18c2f0a1b2c3d4e5"), HIGH, "s"
        )

        self.assertEqual(
            chat.sent[0][1],
            [
                [
                    ("Valider", "fb:v:18c2f0a1b2c3d4e5"),
                    ("Faux-Urgent", "fb:u:18c2f0a1b2c3d4e5"),
                    ("Faux-Spam", "fb:s:18c2f0a1b2c3d4e5"),
                ]
            ],
        )
        self.assertIsNone(webhook.sent[0][1])

    def test_no_buttons_without_the_flag(self):
        channel = FakeChannel()
        AlertGateway([channel]).send_urgent_alert(make_email(), HIGH, "s")

        self.assertIsNone(channel.sent[0][1])

    def test_oversized_id_sends_the_alert_without_buttons(self):
        channel = FakeChannel()
        AlertGateway([channel], feedback_buttons=True).send_urgent_alert(
            make_email(id="x" * 80), HIGH, "s"
        )

        self.assertEqual(len(channel.sent), 1)
        self.assertIsNone(channel.sent[0][1])


class SendTextTests(unittest.TestCase):
    def test_goes_to_configured_interactive_channels_only(self):
        chat, webhook = FakeChannel(), FakeChannel("webhook", False)
        off = FakeChannel("off", configured=False)

        AlertGateway([chat, webhook, off]).send_text("hello")

        self.assertEqual(chat.sent, [("hello", None)])
        self.assertEqual(webhook.sent, [])
        self.assertEqual(off.sent, [])

    def test_failure_is_logged_not_raised(self):
        with self.assertLogs("src.gateways.alerts", level="WARNING"):
            AlertGateway([FakeChannel(error=down())]).send_text("hello")


class ChannelProtocolTests(unittest.TestCase):
    def test_fake_satisfies_the_port(self):
        from src.ports import AlertChannel

        self.assertIsInstance(FakeChannel(), AlertChannel)


if __name__ == "__main__":
    unittest.main()
