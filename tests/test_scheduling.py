import unittest
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.scheduling import DailyJob
from src.storage import SqliteDecisionStore

PARIS = ZoneInfo("Europe/Paris")


class DailyJobTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 1, 5, 59, tzinfo=UTC)

    def job(self, name="list", hour=8):
        return DailyJob(name, hour, PARIS, self.store, clock=lambda: self.now)

    def test_waits_for_the_local_hour_then_runs_once_a_day(self):
        job = self.job()
        # 05:59 UTC is 07:59 in Paris in summer time.
        self.assertFalse(job.claim())

        self.now = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
        self.assertTrue(job.claim())
        self.assertFalse(job.claim())

        self.now = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
        self.assertFalse(job.claim())

    def test_runs_again_the_next_local_day(self):
        self.now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        self.assertTrue(self.job().claim())

        # 22:30 UTC is already tomorrow in Paris, but before the hour.
        self.now = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)
        self.assertFalse(self.job().claim())

        self.now = datetime(2026, 10, 2, 6, 30, tzinfo=UTC)
        self.assertTrue(self.job().claim())

    def test_a_restart_does_not_repeat_the_run(self):
        self.now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        self.assertTrue(self.job().claim())

        self.assertFalse(self.job().claim())

    def test_a_late_start_still_runs_that_day(self):
        self.now = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)

        self.assertTrue(self.job().claim())

    def test_jobs_are_tracked_separately(self):
        self.now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

        self.assertTrue(self.job("list").claim())
        self.assertTrue(self.job("other").claim())

    def test_the_last_run_is_not_pruned_with_the_expired_state(self):
        clock = [datetime(2026, 10, 1, 9, 0, tzinfo=UTC)]
        store = SqliteDecisionStore(":memory:", clock=lambda: clock[0])
        self.addCleanup(store.close)
        DailyJob("list", 8, PARIS, store, clock=lambda: clock[0]).claim()
        store.set_state("put_forward_list:last", "2026-10-01T09:00:00+00:00")
        store.set_state("cmd:1", "review")
        clock[0] += timedelta(days=200)

        store.prune(timedelta(days=90))

        self.assertEqual(store.get_state("job:list"), "2026-10-01")
        self.assertIsNotNone(store.get_state("put_forward_list:last"))
        self.assertIsNone(store.get_state("cmd:1"))


if __name__ == "__main__":
    unittest.main()
