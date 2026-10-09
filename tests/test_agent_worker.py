import threading
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.agent.worker import (
    CRASHED,
    INTERRUPTED,
    OVER_BUDGET,
    UNKNOWN_KIND,
    AgentBudget,
    AgentWorker,
)
from src.domain import ANSWERED, RUN_DONE, RUN_FAILED, RUN_QUEUED, Trajectory, Usage
from src.ports import AgentRuns
from src.storage import SqliteDecisionStore

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
PARIS = ZoneInfo("Europe/Paris")


class RecordingHandler:
    def __init__(self, cost=0.01, error=None):
        self.ran = []
        self.given_up = []
        self.cost = cost
        self.error = error

    def run(self, run):
        self.ran.append(run.trigger_key)
        if self.error is not None:
            raise self.error
        return Trajectory(ANSWERED, (), Usage(10, 5, self.cost), "ok")

    def gave_up(self, run, reason):
        self.given_up.append((run.trigger_key, reason))


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.runs = self.store.agent_runs
        self.handler = RecordingHandler()
        metrics = patch("src.agent.worker.Metrics")
        self.metrics = metrics.start()
        self.addCleanup(metrics.stop)

    def budget(self, daily_usd=0.0, daily_runs=100):
        return AgentBudget(self.runs, daily_usd, daily_runs, PARIS, clock=lambda: self.clock[0])

    def worker(self, **budget):
        return AgentWorker(self.runs, {"chat": self.handler}, self.budget(**budget))

    def queue(self, *keys, kind="chat"):
        for key in keys:
            self.runs.enqueue(key, kind, {})


class AgentWorkerTests(WorkerTestCase):
    def test_the_store_offers_the_queue_port(self):
        self.assertIsInstance(self.runs, AgentRuns)

    def test_nothing_queued_is_nothing_done(self):
        self.assertFalse(self.worker().run_one())
        self.assertEqual(self.handler.ran, [])

    def test_a_queued_run_is_carried_out_recorded_and_counted(self):
        self.queue("txt:1")

        self.assertTrue(self.worker().run_one())

        run = self.runs.get("txt:1")
        self.assertEqual((run.state, run.outcome, run.answer), (RUN_DONE, ANSWERED, "ok"))
        self.assertEqual(self.handler.ran, ["txt:1"])
        self.metrics.mark_agent_run.assert_called_once_with("chat", ANSWERED)
        self.metrics.agent_queue.set.assert_called_with(0)

    def test_runs_are_carried_out_one_at_a_time_in_order(self):
        self.queue("txt:1", "txt:2")
        worker = self.worker()

        worker.run_one()

        self.assertEqual(self.handler.ran, ["txt:1"])
        self.assertEqual(self.runs.get("txt:2").state, RUN_QUEUED)

    def test_a_crash_fails_the_run_tells_whoever_asked_and_spares_the_next(self):
        self.queue("txt:1", "txt:2")
        self.handler.error = RuntimeError("boom")
        worker = self.worker()

        with self.assertLogs("src.agent.worker", level="ERROR"):
            worker.run_one()
        self.handler.error = None
        worker.run_one()

        failed = self.runs.get("txt:1")
        self.assertEqual((failed.state, failed.outcome), (RUN_FAILED, CRASHED))
        self.assertEqual(self.handler.given_up, [("txt:1", CRASHED)])
        self.assertEqual(self.runs.get("txt:2").state, RUN_DONE)

    def test_a_run_of_an_unknown_kind_is_failed_not_left_in_the_queue(self):
        self.queue("x:1", kind="telepathy")

        with self.assertLogs("src.agent.worker", level="WARNING"):
            self.assertTrue(self.worker().run_one())

        self.assertEqual(self.runs.get("x:1").outcome, UNKNOWN_KIND)
        self.assertEqual(self.handler.ran, [])

    def test_failing_to_tell_does_not_stop_the_worker(self):
        self.queue("txt:1")
        self.handler.error = RuntimeError("boom")
        self.handler.gave_up = lambda run, reason: (_ for _ in ()).throw(RuntimeError("chat down"))

        with self.assertLogs("src.agent.worker", level="ERROR"):
            self.assertTrue(self.worker().run_one())

        self.assertEqual(self.runs.get("txt:1").state, RUN_FAILED)


class RecoveryTests(WorkerTestCase):
    def test_a_run_cut_short_by_a_stop_is_failed_and_reported_not_replayed(self):
        self.queue("txt:1", "txt:2")
        self.runs.take_next()

        self.worker().recover()

        self.assertEqual(self.runs.get("txt:1").outcome, INTERRUPTED)
        self.assertEqual(self.handler.given_up, [("txt:1", INTERRUPTED)])
        self.assertEqual(self.handler.ran, [])
        self.assertEqual(self.runs.get("txt:2").state, RUN_QUEUED)

    def test_the_loop_recovers_then_empties_the_queue_until_told_to_stop(self):
        self.queue("txt:1", "txt:2", "txt:3")
        self.runs.take_next()
        worker = self.worker()
        stopping = threading.Event()
        emptied = threading.Event()
        run = self.handler.run

        def run_then_signal(agent_run):
            trajectory = run(agent_run)
            if agent_run.trigger_key == "txt:3":
                emptied.set()
            return trajectory

        self.handler.run = run_then_signal
        thread = threading.Thread(target=worker.run_forever, args=(stopping,))
        thread.start()
        self.assertTrue(emptied.wait(5))
        stopping.set()
        worker.notify()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.handler.ran, ["txt:2", "txt:3"])
        self.assertEqual(self.handler.given_up, [("txt:1", INTERRUPTED)])

    def test_a_notification_wakes_an_idle_worker(self):
        worker = self.worker()
        stopping = threading.Event()
        done = threading.Event()
        run = self.handler.run
        self.handler.run = lambda agent_run: (run(agent_run), done.set())[0]
        thread = threading.Thread(target=worker.run_forever, args=(stopping,))
        thread.start()

        self.queue("txt:1")
        worker.notify()

        self.assertTrue(done.wait(2))
        stopping.set()
        worker.notify()
        thread.join(5)


class AgentBudgetTests(WorkerTestCase):
    def spend(self, key, cost):
        self.queue(key)
        self.handler.cost = cost
        self.worker().run_one()

    def test_without_a_cost_cap_only_the_number_of_runs_counts(self):
        self.spend("txt:1", 50.0)

        self.assertFalse(self.budget().exhausted())
        self.assertTrue(self.budget(daily_runs=1).exhausted())

    def test_the_cost_cap_is_reached_by_the_days_runs(self):
        self.spend("txt:1", 0.03)
        self.assertFalse(self.budget(daily_usd=0.05).exhausted())

        self.spend("txt:2", 0.03)

        self.assertTrue(self.budget(daily_usd=0.05).exhausted())

    def test_a_run_of_unknown_price_still_counts_as_a_run(self):
        self.spend("txt:1", None)

        self.assertFalse(self.budget(daily_usd=0.05).exhausted())
        self.assertTrue(self.budget(daily_usd=0.05, daily_runs=1).exhausted())

    def test_the_day_is_the_local_one(self):
        # 23:30 in Paris on the 9th.
        self.clock[0] = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)
        self.spend("txt:1", 0.1)
        self.assertTrue(self.budget(daily_usd=0.05).exhausted())

        self.clock[0] += timedelta(hours=1)

        self.assertFalse(self.budget(daily_usd=0.05).exhausted())

    def test_past_the_budget_a_run_is_refused_and_whoever_asked_is_told(self):
        self.spend("txt:1", 0.1)
        self.queue("txt:2")
        self.handler.ran.clear()

        self.assertTrue(self.worker(daily_usd=0.05).run_one())

        refused = self.runs.get("txt:2")
        self.assertEqual((refused.state, refused.outcome), (RUN_FAILED, OVER_BUDGET))
        self.assertEqual(self.handler.ran, [])
        self.assertEqual(self.handler.given_up, [("txt:2", OVER_BUDGET)])

    def test_the_last_run_of_the_day_is_carried_out(self):
        self.queue("txt:1", "txt:2")
        worker = self.worker(daily_runs=1)

        worker.run_one()
        worker.run_one()

        self.assertEqual(self.runs.get("txt:1").state, RUN_DONE)
        self.assertEqual(self.runs.get("txt:2").outcome, OVER_BUDGET)


if __name__ == "__main__":
    unittest.main()
