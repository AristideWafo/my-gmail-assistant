import unittest
from dataclasses import replace

from main import ApplicationContext, poll_once
from src.config import Settings
from src.health import OutageNotifier
from src.health.outage import format_outage, format_recovery
from tests.fakes import FakeChannel, FakeMail, fake_components


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__("https://api.example/secret-token")
        self.resp = type("Resp", (), {"status": status})()


class OutageNotifierTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sent: list[str] = []
        self.delivers = True
        self.notifier = self.make(alert_after_seconds=600)

    def make(self, alert_after_seconds):
        return OutageNotifier(alert_after_seconds, self.notify, clock=lambda: self.now)

    def notify(self, text):
        self.sent.append(text)
        return self.delivers

    def fail_at(self, *times):
        for now in times:
            self.now = now
            self.notifier.record_failure(RuntimeError("down"))

    def test_short_outage_stays_silent_in_both_directions(self):
        self.fail_at(0, 60, 599)
        self.now = 599
        self.notifier.record_success()

        self.assertEqual(self.sent, [])

    def test_lasting_outage_is_announced_once(self):
        self.fail_at(0, 300, 600, 660, 720)

        self.assertEqual(self.sent, [format_outage(600, RuntimeError("down"))])

    def test_recovery_is_announced_with_the_outage_duration(self):
        self.fail_at(0, 600)
        self.now = 1500
        self.notifier.record_success()

        self.assertEqual(self.sent[1:], [format_recovery(1500)])

    def test_undelivered_announcement_is_retried_on_the_next_failure(self):
        self.delivers = False
        self.fail_at(0, 600, 660)
        self.delivers = True
        self.fail_at(720, 780)

        self.assertEqual(len(self.sent), 3)

    def test_recovery_is_announced_even_when_the_outage_message_never_got_through(self):
        self.delivers = False
        self.fail_at(0, 600)
        self.delivers = True
        self.now = 900
        self.notifier.record_success()

        self.assertEqual(self.sent[-1], format_recovery(900))

    def test_a_new_outage_after_recovery_starts_its_own_clock(self):
        self.fail_at(0, 600)
        self.now = 700
        self.notifier.record_success()
        self.sent.clear()

        self.fail_at(800, 1300)
        self.assertEqual(self.sent, [])
        self.fail_at(1400)
        self.assertEqual(len(self.sent), 1)

    def test_success_without_a_prior_failure_sends_nothing(self):
        self.notifier.record_success()

        self.assertEqual(self.sent, [])

    def test_zero_threshold_disables_it(self):
        self.notifier = self.make(alert_after_seconds=0)

        self.fail_at(0, 10_000)
        self.notifier.record_success()

        self.assertFalse(self.notifier.enabled)
        self.assertEqual(self.sent, [])

    def test_a_raising_notifier_never_propagates(self):
        def boom(text):
            raise RuntimeError("chat exploded")

        notifier = OutageNotifier(600, boom, clock=lambda: self.now)

        with self.assertLogs("src.health.outage", level="ERROR"):
            notifier.record_failure(RuntimeError("down"))
            self.now = 600
            notifier.record_failure(RuntimeError("down"))


class OutageMessageTests(unittest.TestCase):
    def test_outage_message_names_the_error_type_and_status_but_never_its_text(self):
        text = format_outage(725, FakeHttpError(401))

        self.assertIn("depuis 12 min", text)
        self.assertIn("FakeHttpError (HTTP 401)", text)
        self.assertNotIn("secret-token", text)

    def test_durations_round_to_at_least_one_minute(self):
        self.assertIn("après 1 min", format_recovery(5))


class FlakyMail(FakeMail):
    failing = True

    def fetch_unread(self):
        if self.failing:
            raise RuntimeError("invalid_grant")
        return []


class PollingOutageTests(unittest.TestCase):
    def test_a_lasting_fetch_failure_reaches_the_chat_then_the_recovery_does(self):
        now = [0.0]
        chat, mail = FakeChannel(name="chat", interactive=True), FlakyMail()
        settings = Settings(_env_file=None, db_path=":memory:", poll_failure_alert_minutes=10)
        ctx = ApplicationContext(
            settings, replace(fake_components(), mail=mail, channels=(chat,))
        )
        self.addCleanup(ctx.close)
        ctx.outage = OutageNotifier(600, ctx.alerts.send_text, clock=lambda: now[0])

        with self.assertLogs("gmail-assistant", level="ERROR"):
            poll_once(ctx)
            now[0] = 600
            poll_once(ctx)
        mail.failing = False
        now[0] = 900
        poll_once(ctx)

        self.assertEqual(len(chat.sent), 2)
        self.assertIn("en échec depuis 10 min : RuntimeError", chat.sent[0])
        self.assertIn("rétablie après 15 min", chat.sent[1])

    def test_threshold_comes_from_the_settings(self):
        settings = Settings(_env_file=None, db_path=":memory:", poll_failure_alert_minutes=0)
        ctx = ApplicationContext(settings, fake_components())
        self.addCleanup(ctx.close)

        self.assertFalse(ctx.outage.enabled)


if __name__ == "__main__":
    unittest.main()
