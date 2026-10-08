import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.domain import CLOSED, IGNORED, WAITING_FOR_THEM, ThreadRef
from src.followup.refresh import ACTIVATED_AT_KEY, FollowUpTracker
from src.followup.state import ANSWERED, BEFORE_ACTIVATION, DELETED, EXPIRED
from src.observability.metrics import Metrics
from src.storage import SqliteDecisionStore
from tests.fakes import FakeMail
from tests.followup_helpers import MINE, message, snapshot

PARIS = ZoneInfo("Europe/Paris")


class FollowUpTrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.mail = FakeMail(addresses=MINE)
        self.tracker = self.make_tracker()
        # Activation before the test mails, which are sent on 5 October.
        self.tracker.activated_at()
        self.now = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)

    def make_tracker(self, max_threads=50, mail=None):
        return FollowUpTracker(
            mail or self.mail, self.store, PARIS, after_days=3, max_threads=max_threads,
            clock=lambda: self.now,
        )

    def stored(self, thread_id="t1"):
        return self.store.threads.get(thread_id)

    def test_activation_is_dated_once_and_kept(self):
        self.assertEqual(self.store.get_state(ACTIVATED_AT_KEY), "2026-10-01T08:00:00+00:00")
        self.assertEqual(self.make_tracker().activated_at(), datetime(2026, 10, 1, 8, 0, tzinfo=UTC))

    def test_a_sent_mail_without_answer_is_tracked_with_its_due_date(self):
        self.mail.threads = [snapshot(message("m1"))]

        self.tracker.refresh()

        thread = self.stored()
        self.assertEqual((thread.state, thread.anchor.message_id), (WAITING_FOR_THEM, "m1"))
        # Monday 5 October 11:00 in Paris, plus three weekdays.
        self.assertEqual(thread.due_at, datetime(2026, 10, 8, 11, 0, tzinfo=PARIS))

    def test_mail_sent_before_activation_is_not_followed(self):
        self.mail.threads = [snapshot(replace(message("m1"), sent_at=datetime(2026, 9, 30, tzinfo=UTC)))]

        self.tracker.refresh()

        self.assertEqual((self.stored().state, self.stored().reason), (IGNORED, BEFORE_ACTIVATION))

    def test_an_unchanged_thread_is_not_read_again(self):
        self.mail.threads = [snapshot(message("m1"), history_id="1")]
        self.tracker.refresh()
        self.mail.thread_snapshot = MagicMock(side_effect=AssertionError("read again"))

        self.tracker.refresh()

    def test_a_changed_thread_is_read_again(self):
        self.mail.threads = [snapshot(message("m1"), history_id="1")]
        self.tracker.refresh()
        self.mail.threads = [snapshot(message("m1"), message("m2", "jean@example.com", day=6), history_id="2")]

        self.tracker.refresh()

        self.assertEqual((self.stored().state, self.stored().reason), (CLOSED, ANSWERED))

    def test_a_waiting_thread_out_of_the_window_is_still_read_by_its_id(self):
        self.mail.threads = [snapshot(message("m1"))]
        self.tracker.refresh()
        answered = snapshot(message("m1"), message("m2", "jean@example.com", day=6), history_id="2")
        self.mail.sent_threads = lambda newer_than_days, limit: []
        self.mail.threads = [answered]

        self.tracker.refresh()

        self.assertEqual(self.stored().reason, ANSWERED)

    def test_a_thread_waiting_for_a_month_is_closed_without_reading_it(self):
        self.mail.threads = [snapshot(message("m1"))]
        self.tracker.refresh()
        self.mail.sent_threads = lambda newer_than_days, limit: []
        self.mail.thread_snapshot = MagicMock(side_effect=AssertionError("read"))
        self.now += timedelta(days=31)

        self.tracker.refresh()

        self.assertEqual((self.stored().state, self.stored().reason), (CLOSED, EXPIRED))

    def test_a_deleted_thread_is_closed(self):
        self.mail.threads = [snapshot(message("m1"))]
        self.tracker.refresh()
        self.mail.sent_threads = lambda newer_than_days, limit: [ThreadRef("t1", "2")]
        self.mail.threads = []

        self.tracker.refresh()

        self.assertEqual(self.stored().reason, DELETED)

    def test_a_deleted_thread_never_seen_leaves_nothing(self):
        self.mail.sent_threads = lambda newer_than_days, limit: [ThreadRef("t9", "1")]

        self.tracker.refresh()

        self.assertIsNone(self.stored("t9"))

    def test_only_the_most_recent_threads_up_to_the_cap_are_followed(self):
        self.mail.threads = [snapshot(message(f"m{i}"), thread_id=f"t{i}") for i in range(3)]

        self.make_tracker(max_threads=2).refresh()

        self.assertEqual(self.store.threads.counts(), {WAITING_FOR_THEM: 2})
        self.assertEqual(Metrics.followup_capped._value.get(), 1)

        self.make_tracker(max_threads=3).refresh()
        self.assertEqual(Metrics.followup_capped._value.get(), 0)

    def test_waiting_threads_read_outside_the_listing_are_counted(self):
        self.mail.threads = [snapshot(message("m1"))]
        self.tracker.refresh()
        self.mail.sent_threads = lambda newer_than_days, limit: []

        self.tracker.refresh()

        self.assertEqual(Metrics.followup_unlisted_waiting._value.get(), 1)

    def test_a_dismissal_written_during_the_read_is_not_overwritten(self):
        self.mail.threads = [snapshot(message("m1"), history_id="1")]
        self.tracker.refresh()
        self.mail.threads = [snapshot(message("m1"), history_id="2")]
        real = self.mail.thread_snapshot

        def read_while_the_chat_dismisses(thread_id):
            self.store.threads.update(
                thread_id,
                lambda t: replace(t, state=CLOSED, reason="dismissed", proposal_state="dismissed"),
            )
            return real(thread_id)

        self.mail.thread_snapshot = read_while_the_chat_dismisses
        self.tracker.refresh()

        self.assertEqual((self.stored().state, self.stored().reason), (CLOSED, "dismissed"))
        self.assertEqual(self.stored().history_id, "2")

    def test_the_due_date_is_stored_in_utc(self):
        self.mail.threads = [snapshot(message("m1"))]

        self.tracker.refresh()

        raw = self.store._conn.execute("SELECT due_at FROM threads").fetchone()[0]
        self.assertEqual(raw, "2026-10-08T09:00:00+00:00")

    def test_an_unreadable_activation_date_starts_again_from_now(self):
        for raw in ("yesterday", "2026-10-01T08:00:00"):
            with self.subTest(raw=raw):
                self.store.set_state(ACTIVATED_AT_KEY, raw)
                with self.assertLogs("src.followup.refresh", level="WARNING"):
                    self.assertEqual(self.tracker.activated_at(), self.now)
                self.assertEqual(self.tracker.activated_at(), self.now)

    def test_a_listing_failure_is_counted_and_reads_nothing(self):
        mail = MagicMock()
        mail.sent_threads.side_effect = RuntimeError("quota")
        before = Metrics.total(Metrics.followup_refresh_errors)

        with self.assertLogs("src.followup.refresh", level="WARNING"):
            self.make_tracker(mail=mail).refresh()

        mail.thread_snapshot.assert_not_called()
        self.assertEqual(Metrics.total(Metrics.followup_refresh_errors), before + 1)

    def test_one_unreadable_thread_does_not_stop_the_others(self):
        good = snapshot(message("m2"), thread_id="t2")
        self.mail.threads = [snapshot(message("m1")), good]
        real = self.mail.thread_snapshot

        def flaky(thread_id):
            if thread_id == "t1":
                raise RuntimeError("500")
            return real(thread_id)

        self.mail.thread_snapshot = flaky
        with self.assertLogs("src.followup.refresh", level="WARNING"):
            self.tracker.refresh()

        self.assertIsNone(self.stored("t1"))
        self.assertEqual(self.stored("t2").state, WAITING_FOR_THEM)

    def test_the_state_counts_are_published(self):
        self.mail.threads = [snapshot(message("m1"))]

        self.tracker.refresh()

        self.assertEqual(Metrics.followup_threads.labels(state=WAITING_FOR_THEM)._value.get(), 1)


if __name__ == "__main__":
    unittest.main()
