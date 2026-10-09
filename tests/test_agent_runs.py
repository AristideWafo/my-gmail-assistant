import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta

from src.domain import (
    ANSWERED,
    RUN_DONE,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    AgentTurn,
    ToolCall,
    ToolResult,
    ToolResults,
    Trajectory,
    Usage,
    UserMessage,
)
from src.storage import SqliteDecisionStore
from src.storage.agent_runs import TRACE_VALUE_CHARS
from src.storage.migrations import LATEST_VERSION, MIGRATIONS, schema_version

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
SEARCH = ToolCall("search_mail", {"query": "devis"})
TRAJECTORY = Trajectory(
    ANSWERED,
    (
        UserMessage("où en est le devis ?"),
        AgentTurn(text="Je cherche.", tool_calls=(SEARCH,)),
        ToolResults((ToolResult(SEARCH, "x" * 1000), ToolResult(SEARCH, "refused", is_error=True))),
        AgentTurn(text="2 480 euros."),
    ),
    Usage(300, 40, 0.002),
    "2 480 euros.",
)


class AgentRunsTests(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.runs = self.store.agent_runs

    def row(self, trigger_key):
        return self.store._conn.execute(
            "SELECT * FROM agent_runs WHERE trigger_key = ?", (trigger_key,)
        ).fetchone()

    def test_a_trigger_queues_a_single_run(self):
        self.assertTrue(self.runs.enqueue("txt:1", "chat", {"text": "où en est le devis ?"}))
        self.assertFalse(self.runs.enqueue("txt:1", "chat", {"text": "encore"}))

        run = self.runs.get("txt:1")
        self.assertEqual(
            (run.kind, run.payload, run.state, run.created_at),
            ("chat", {"text": "où en est le devis ?"}, RUN_QUEUED, NOW),
        )
        self.assertEqual(self.runs.queued(), 1)

    def test_an_unknown_trigger_has_no_run(self):
        self.assertIsNone(self.runs.get("txt:nope"))

    def test_runs_are_taken_oldest_first_and_once(self):
        self.runs.enqueue("txt:1", "chat", {})
        self.runs.enqueue("txt:2", "chat", {})

        first, second = self.runs.take_next(), self.runs.take_next()

        self.assertEqual((first.trigger_key, first.state), ("txt:1", RUN_RUNNING))
        self.assertEqual(second.trigger_key, "txt:2")
        self.assertIsNone(self.runs.take_next())
        self.assertEqual(self.runs.queued(), 0)

    def test_concurrent_takers_never_share_a_run(self):
        for i in range(20):
            self.runs.enqueue(f"txt:{i}", "chat", {})
        start = threading.Barrier(4)
        taken = []

        def take():
            start.wait()
            while (run := self.runs.take_next()) is not None:
                taken.append(run.id)

        threads = [threading.Thread(target=take) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(taken), sorted(set(taken)))
        self.assertEqual(len(taken), 20)

    def test_finishing_records_the_answer_the_usage_and_a_cut_trace(self):
        self.runs.enqueue("txt:1", "chat", {})
        run = self.runs.take_next()
        self.clock[0] = NOW + timedelta(seconds=8)

        self.runs.finish(run.id, TRAJECTORY)

        done = self.runs.get("txt:1")
        self.assertEqual(
            (done.state, done.outcome, done.answer), (RUN_DONE, ANSWERED, "2 480 euros.")
        )
        row = self.row("txt:1")
        self.assertEqual(
            (row["input_tokens"], row["output_tokens"], row["cost_usd"]), (300, 40, 0.002)
        )
        self.assertEqual(row["finished_at"], (NOW + timedelta(seconds=8)).isoformat())
        said, found, refused, answered = json.loads(row["trace"])
        self.assertEqual(said, {"said": "Je cherche."})
        self.assertEqual(
            (found["tool"], found["args"], found["error"]),
            ("search_mail", '{"query": "devis"}', False),
        )
        self.assertEqual(len(found["result"]), TRACE_VALUE_CHARS + 1)
        self.assertTrue(refused["error"])
        self.assertEqual(answered, {"said": "2 480 euros."})

    def test_a_run_not_running_cannot_be_finished(self):
        self.runs.enqueue("txt:1", "chat", {})

        self.runs.finish(self.runs.get("txt:1").id, TRAJECTORY)

        self.assertEqual(self.runs.get("txt:1").state, RUN_QUEUED)

    def test_a_failed_run_keeps_why_and_is_not_overwritten_by_a_late_finish(self):
        self.runs.enqueue("txt:1", "chat", {})
        run = self.runs.take_next()

        self.runs.fail(run.id, "crashed")
        self.runs.finish(run.id, TRAJECTORY)

        failed = self.runs.get("txt:1")
        self.assertEqual((failed.state, failed.outcome, failed.answer), (RUN_FAILED, "crashed", ""))

    def test_only_runs_left_running_are_failed_as_interrupted(self):
        for key in ("txt:1", "txt:2", "txt:3"):
            self.runs.enqueue(key, "chat", {})
        done = self.runs.take_next()
        self.runs.finish(done.id, TRAJECTORY)
        self.runs.take_next()

        interrupted = self.runs.fail_interrupted("interrupted")

        self.assertEqual(
            [(r.trigger_key, r.state, r.outcome) for r in interrupted],
            [("txt:2", RUN_FAILED, "interrupted")],
        )
        self.assertEqual(self.runs.get("txt:1").state, RUN_DONE)
        self.assertEqual(self.runs.get("txt:2").state, RUN_FAILED)
        self.assertEqual(self.runs.get("txt:3").state, RUN_QUEUED)

    def test_runs_and_cost_are_counted_from_a_moment(self):
        for i, cost in enumerate((0.01, None, 0.02)):
            self.clock[0] = NOW + timedelta(hours=i)
            self.runs.enqueue(f"txt:{i}", "chat", {})
            run = self.runs.take_next()
            self.runs.finish(run.id, Trajectory(ANSWERED, (), Usage(1, 1, cost)))
        self.runs.enqueue("txt:queued", "chat", {})

        self.assertEqual(self.runs.started_since(NOW), 3)
        self.assertAlmostEqual(self.runs.cost_since(NOW), 0.03)
        self.assertEqual(self.runs.started_since(NOW + timedelta(minutes=30)), 2)
        self.assertAlmostEqual(self.runs.cost_since(NOW + timedelta(minutes=90)), 0.02)
        self.assertEqual(self.runs.cost_since(NOW + timedelta(days=1)), 0)

    def test_prune_forgets_old_finished_runs_and_keeps_what_still_waits(self):
        for key in ("txt:done", "txt:failed", "txt:waiting"):
            self.runs.enqueue(key, "chat", {})
        self.runs.finish(self.runs.take_next().id, TRAJECTORY)
        self.runs.fail(self.runs.take_next().id, "crashed")
        self.clock[0] = NOW + timedelta(days=91)
        self.runs.enqueue("txt:recent", "chat", {})

        self.store.prune(timedelta(days=90))

        self.assertIsNone(self.runs.get("txt:done"))
        self.assertIsNone(self.runs.get("txt:failed"))
        self.assertIsNotNone(self.runs.get("txt:waiting"))
        self.assertIsNotNone(self.runs.get("txt:recent"))


class AgentRunsMigrationTests(unittest.TestCase):
    def test_version_8_database_is_upgraded_keeping_its_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "assistant.db")
        conn = sqlite3.connect(path)
        conn.executescript("".join(MIGRATIONS[:8]) + "PRAGMA user_version = 8;")
        conn.execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES ('k', 'v', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        store = SqliteDecisionStore(path)
        self.addCleanup(store.close)

        self.assertEqual(schema_version(store._conn), LATEST_VERSION)
        self.assertEqual(store.get_state("k"), "v")
        self.assertTrue(store.agent_runs.enqueue("txt:1", "chat", {}))


if __name__ == "__main__":
    unittest.main()
