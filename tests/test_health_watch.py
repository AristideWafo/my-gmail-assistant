import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from prometheus_client import Counter, Gauge

from src.domain import EmailMessage, LLMAnalysis
from src.health import (
    BudgetedAnalyzer,
    Check,
    HealthWatch,
    RecentIncrease,
    SpendTracker,
    budget_check,
    counter_checks,
    listener_check,
)
from src.observability.metrics import Metrics
from src.ports import EmailAnalyzer
from src.storage import SqliteDecisionStore

EMAIL = EmailMessage("m1", "t", "a@b.com", "Hi", "s", "b")


class MetricsTotalTests(unittest.TestCase):
    def test_sums_every_label_set_and_ignores_creation_timestamps(self):
        counter = Counter("test_total_sum", "test", ["reason"])
        counter.labels(reason="a").inc(2)
        counter.labels(reason="b").inc(3)

        self.assertEqual(Metrics.total(counter), 5)
        self.assertEqual(Metrics.total(counter, without={"reason": "b"}), 2)

    def test_reads_a_gauge_and_an_unlabelled_counter(self):
        gauge = Gauge("test_total_gauge", "test")
        gauge.set(42)
        counter = Counter("test_total_plain", "test")

        self.assertEqual(Metrics.total(gauge), 42)
        self.assertEqual(Metrics.total(counter), 0)


class RecentIncreaseTests(unittest.TestCase):
    def setUp(self):
        self.now, self.count = 0.0, 0.0
        self.increase = RecentIncrease(lambda: self.count, 900, clock=lambda: self.now)

    def at(self, now, count):
        self.now, self.count = now, count
        return self.increase.value()

    def test_first_reading_is_the_baseline(self):
        self.assertEqual(self.at(0, 7), 0)

    def test_counts_what_was_added_inside_the_window(self):
        self.at(0, 7)

        self.assertEqual(self.at(60, 9), 2)
        self.assertEqual(self.at(600, 12), 5)

    def test_forgets_what_is_older_than_the_window(self):
        self.at(0, 0)
        self.at(60, 3)

        self.assertEqual(self.at(900, 3), 3)
        self.assertEqual(self.at(961, 3), 0)

    def test_a_long_gap_between_readings_keeps_the_last_reading_as_baseline(self):
        self.at(0, 0)
        self.at(60, 3)

        self.assertEqual(self.at(5000, 4), 1)


class HealthWatchTests(unittest.TestCase):
    def setUp(self):
        self.problem = None
        self.sent = []
        self.delivered = True
        self.watch = HealthWatch(
            [Check("jev", lambda: self.problem, "back")], self.notify
        )

    def notify(self, text):
        self.sent.append(text)
        return self.delivered

    def test_says_nothing_while_all_is_well(self):
        self.watch.run()

        self.assertEqual(self.sent, [])

    def test_a_problem_is_announced_once_then_its_end(self):
        self.problem = "down"
        self.watch.run()
        self.watch.run()
        self.problem = None
        self.watch.run()
        self.watch.run()

        self.assertEqual(self.sent, ["down", "back"])

    def test_an_undelivered_announcement_is_tried_again_and_no_recovery_follows_a_silence(self):
        self.problem, self.delivered = "down", False
        self.watch.run()
        self.problem = None
        self.watch.run()

        self.assertEqual(self.sent, ["down"])

        self.problem = "down"
        self.watch.run()
        self.delivered = True
        self.watch.run()
        self.assertEqual(self.sent, ["down", "down", "down"])

    def test_a_problem_that_ends_by_itself_has_no_recovery_message(self):
        watch = HealthWatch([Check("budget", lambda: self.problem)], self.notify)
        self.problem = "spent"
        watch.run()
        self.problem = None
        watch.run()

        self.assertEqual(self.sent, ["spent"])

    def test_a_check_or_a_send_that_raises_does_not_stop_the_others(self):
        def broken():
            raise RuntimeError("boom")

        notify = MagicMock(side_effect=[RuntimeError("telegram down"), True])
        watch = HealthWatch(
            [Check("broken", broken), Check("a", lambda: "a down"), Check("b", lambda: "b down")],
            notify,
        )

        with self.assertLogs("src.health.watch", level="ERROR"):
            watch.run()

        self.assertEqual([call.args[0] for call in notify.call_args_list], ["a down", "b down"])


class CounterChecksTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        checks = counter_checks(900, 3, clock=lambda: self.now)
        self.checks = {check.name: check for check in checks}

    def test_one_check_per_failure_counter(self):
        self.assertEqual(set(self.checks), {"jev_fallback", "emails_skipped", "llm_errors"})
        self.assertTrue(all(check.recovery for check in self.checks.values()))

    def test_a_burst_of_fallbacks_is_a_problem_until_the_window_has_passed(self):
        check = self.checks["jev_fallback"]
        self.assertIsNone(check.problem())

        for _ in range(2):
            Metrics.mark_jev_fallback()
        self.now = 60
        self.assertIsNone(check.problem())

        Metrics.mark_jev_fallback()
        self.now = 120
        self.assertIn("3 mails classés par l'heuristique de repli en 15 min", check.problem())

        self.now = 2000
        self.assertIsNone(check.problem())

    def test_skipped_mails_and_llm_errors_are_watched_the_same_way(self):
        for check in self.checks.values():
            check.problem()
        for _ in range(3):
            Metrics.mark_email_skipped()
            Metrics.mark_llm_error("timeout")
        self.now = 60

        self.assertIn("3 traitements de mail en échec", self.checks["emails_skipped"].problem())
        self.assertIn("Gemini en erreur : 3 appels", self.checks["llm_errors"].problem())

    def test_calls_skipped_for_the_budget_are_not_llm_errors(self):
        check = self.checks["llm_errors"]
        check.problem()
        for _ in range(5):
            Metrics.mark_llm_error("budget")
        self.now = 60

        self.assertIsNone(check.problem())


class ListenerCheckTests(unittest.TestCase):
    def setUp(self):
        self.alive = True
        self.now = 1_000_000.0
        self.addCleanup(Metrics.telegram_last_poll.set, Metrics.total(Metrics.telegram_last_poll))
        Metrics.telegram_last_poll.set(0)
        self.check = listener_check(lambda: self.alive, clock=lambda: self.now)

    def test_fine_while_the_listener_polls(self):
        Metrics.telegram_last_poll.set(self.now - 30)

        self.assertIsNone(self.check.problem())

    def test_a_listener_that_stopped_polling_is_a_problem(self):
        Metrics.telegram_last_poll.set(self.now - 900)

        self.assertIn("aucune relève réussie depuis 15 min", self.check.problem())

    def test_a_dead_listener_is_a_problem_at_once(self):
        Metrics.telegram_last_poll.set(self.now - 30)
        self.alive = False

        self.assertIsNotNone(self.check.problem())

    def test_a_listener_that_never_polled_gets_the_same_delay_from_the_start(self):
        self.assertIsNone(self.check.problem())

        self.now += 601
        self.assertIsNotNone(self.check.problem())


class SpendTrackerTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.cost = 5.0
        self.now = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    def tracker(self, budget=1.0):
        return SpendTracker(
            self.store, lambda: self.cost, budget, ZoneInfo("Europe/Paris"), clock=lambda: self.now
        )

    def test_only_what_is_spent_after_the_start_counts(self):
        tracker = self.tracker()

        self.assertEqual(tracker.spent_today(), 0)
        self.cost += 0.25
        self.assertAlmostEqual(tracker.spent_today(), 0.25)
        self.assertAlmostEqual(tracker.spent_today(), 0.25)

    def test_the_day_total_survives_a_restart_that_resets_the_counter(self):
        tracker = self.tracker()
        self.cost += 0.4
        tracker.spent_today()

        self.cost = 0.0
        restarted = self.tracker()
        self.cost += 0.3

        self.assertAlmostEqual(restarted.spent_today(), 0.7)

    def test_a_new_local_day_starts_from_zero(self):
        tracker = self.tracker()
        self.cost += 0.9
        tracker.spent_today()

        # 22:30 UTC is already the next day in Paris.
        self.now = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)
        self.cost += 0.2

        self.assertAlmostEqual(tracker.spent_today(), 0.2)

    def test_over_budget_once_the_day_total_reaches_the_cap(self):
        tracker = self.tracker(budget=1.0)
        self.cost += 0.75
        self.assertFalse(tracker.over_budget())

        self.cost += 0.5
        self.assertTrue(tracker.over_budget())

    def test_no_cap_means_never_over_budget_and_nothing_stored(self):
        tracker = self.tracker(budget=0)
        self.cost += 100

        self.assertFalse(tracker.over_budget())
        self.assertIsNone(self.store.get_state("llm_spend:2026-10-01"))

    def test_old_day_totals_are_pruned(self):
        tracker = self.tracker()
        self.cost += 0.5
        tracker.spent_today()
        self.store._clock = lambda: datetime.now(UTC) + timedelta(days=200)

        self.store.prune(timedelta(days=90))

        self.assertIsNone(self.store.get_state("llm_spend:2026-10-01"))


class BudgetedAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.inner = MagicMock()
        self.inner.analyze.return_value = LLMAnalysis(summary="s", draft="d")
        self.spend = MagicMock()
        self.analyzer = BudgetedAnalyzer(self.inner, self.spend)

    def test_is_an_analyzer_and_forwards_its_identity(self):
        self.inner.is_configured = True
        self.inner.check_connection.return_value = "model available"

        self.assertIsInstance(self.analyzer, EmailAnalyzer)
        self.assertTrue(self.analyzer.is_configured)
        self.assertEqual(self.analyzer.check_connection(), "model available")

    def test_calls_the_analyzer_under_budget(self):
        self.spend.over_budget.return_value = False

        result = self.analyzer.analyze(EMAIL, True, False)

        self.assertEqual(result.draft, "d")
        self.inner.analyze.assert_called_once_with(EMAIL, True, False)

    def test_over_budget_returns_an_empty_analysis_without_calling_and_counts_it(self):
        self.spend.over_budget.return_value = True
        before = Metrics.llm_errors.labels(reason="budget")._value.get()

        with self.assertLogs("src.health.spend", level="WARNING"):
            result = self.analyzer.analyze(EMAIL, True, False)

        self.assertEqual(result, LLMAnalysis())
        self.inner.analyze.assert_not_called()
        self.assertEqual(Metrics.llm_errors.labels(reason="budget")._value.get(), before + 1)


class BudgetCheckTests(unittest.TestCase):
    def test_says_how_much_was_spent_and_has_no_recovery(self):
        spend = MagicMock(daily_budget_usd=0.5)
        spend.over_budget.return_value = True
        spend.spent_today.return_value = 0.52
        check = budget_check(spend)

        self.assertIn("0.52 $ sur 0.50 $", check.problem())
        self.assertIsNone(check.recovery)

        spend.over_budget.return_value = False
        self.assertIsNone(check.problem())


if __name__ == "__main__":
    unittest.main()
