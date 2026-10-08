import unittest
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.scheduling import ProactiveBudget
from src.storage import SqliteDecisionStore

PARIS = ZoneInfo("Europe/Paris")


class ProactiveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        # 10:00 in Paris.
        self.now = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    def budget(self, cap=3, quiet_hours=None):
        return ProactiveBudget(self.store, PARIS, cap, quiet_hours, clock=lambda: self.now)

    def test_the_cap_counts_the_messages_sent_today(self):
        budget = self.budget(cap=3)
        self.assertEqual(budget.available(), 3)

        budget.spend()
        budget.spend(units=2)

        self.assertEqual(budget.available(), 0)

    def test_only_spent_units_count(self):
        budget = self.budget(cap=2)

        budget.available()
        budget.available()

        self.assertEqual(budget.available(), 2)

    def test_the_count_survives_a_restart(self):
        self.budget(cap=2).spend()

        self.assertEqual(self.budget(cap=2).available(), 1)

    def test_a_new_local_day_starts_a_new_count(self):
        self.budget(cap=1).spend()

        # 22:30 UTC is already the next day in Paris.
        self.now = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)

        self.assertEqual(self.budget(cap=1).available(), 1)

    def test_nothing_goes_out_during_quiet_hours_across_midnight(self):
        budget = self.budget(quiet_hours=(22, 8))
        for utc_hour, expected in ((19, 3), (20, 0), (23, 0), (5, 0), (6, 3)):
            with self.subTest(utc_hour=utc_hour):
                self.now = datetime(2026, 10, 1, utc_hour, 0, tzinfo=UTC)
                self.assertEqual(budget.available(), expected)

    def test_overspending_never_goes_negative(self):
        budget = self.budget(cap=1)

        budget.spend(units=3)

        self.assertEqual(budget.available(), 0)

    def test_old_counts_are_pruned(self):
        self.budget().spend()
        self.now += timedelta(days=200)

        self.store.prune(timedelta(days=90))

        self.assertIsNone(self.store.get_state("proactive:2026-10-01"))


if __name__ == "__main__":
    unittest.main()
