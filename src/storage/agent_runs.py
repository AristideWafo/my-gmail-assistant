import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.domain import (
    ANSWERED,
    RUN_DONE,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    AgentRun,
    AgentTurn,
    ToolResults,
    Trajectory,
)

_COLUMNS = "id, trigger_key, kind, payload, state, outcome, answer, created_at"
_SELECT = f"SELECT {_COLUMNS} FROM agent_runs"
# What a run did is kept to understand it afterwards, not to replay it: enough of each argument
# and result to tell what happened.
TRACE_VALUE_CHARS = 300


class SqliteAgentRuns:
    """The agent's queue and its record; shares the decision store's connection and lock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock,
        clock: Callable[[], datetime],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._clock = clock

    def enqueue(self, trigger_key: str, kind: str, payload: dict) -> bool:
        with self._lock, self._conn:
            return (
                self._conn.execute(
                    "INSERT OR IGNORE INTO agent_runs "
                    "(trigger_key, kind, payload, state, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        trigger_key,
                        kind,
                        json.dumps(payload, ensure_ascii=False),
                        RUN_QUEUED,
                        self._now(),
                    ),
                ).rowcount
                == 1
            )

    def get(self, trigger_key: str) -> AgentRun | None:
        with self._lock:
            row = self._conn.execute(f"{_SELECT} WHERE trigger_key = ?", (trigger_key,)).fetchone()
        return None if row is None else _to_run(row)

    def answered_before(self, run: AgentRun, since: datetime, limit: int) -> list[AgentRun]:
        with self._lock:
            rows = self._conn.execute(
                f"{_SELECT} WHERE kind = ? AND state = ? AND outcome = ? AND id < ? "
                "AND created_at >= ? ORDER BY id DESC LIMIT ?",
                (run.kind, RUN_DONE, ANSWERED, run.id, _iso(since), limit),
            ).fetchall()
        return [_to_run(row) for row in reversed(rows)]

    def take_next(self) -> AgentRun | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                f"{_SELECT} WHERE state = ? ORDER BY id LIMIT 1", (RUN_QUEUED,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE agent_runs SET state = ?, started_at = ? WHERE id = ?",
                (RUN_RUNNING, self._now(), row["id"]),
            )
        return _to_run(row, state=RUN_RUNNING)

    def finish(self, run_id: int, trajectory: Trajectory) -> None:
        usage = trajectory.usage
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE agent_runs SET state = ?, outcome = ?, answer = ?, trace = ?, "
                "input_tokens = ?, output_tokens = ?, cost_usd = ?, finished_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    RUN_DONE,
                    trajectory.outcome,
                    trajectory.answer,
                    json.dumps(_trace(trajectory), ensure_ascii=False),
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cost_usd,
                    self._now(),
                    run_id,
                    RUN_RUNNING,
                ),
            )

    def fail(self, run_id: int, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE agent_runs SET state = ?, outcome = ?, finished_at = ? "
                "WHERE id = ? AND state IN (?, ?)",
                (RUN_FAILED, reason, self._now(), run_id, RUN_QUEUED, RUN_RUNNING),
            )

    def fail_interrupted(self, reason: str) -> list[AgentRun]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                f"{_SELECT} WHERE state = ? ORDER BY id", (RUN_RUNNING,)
            ).fetchall()
            self._conn.execute(
                "UPDATE agent_runs SET state = ?, outcome = ?, finished_at = ? WHERE state = ?",
                (RUN_FAILED, reason, self._now(), RUN_RUNNING),
            )
        return [_to_run(row, state=RUN_FAILED, outcome=reason) for row in rows]

    def started_since(self, moment: datetime) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE started_at >= ?", (_iso(moment),)
            ).fetchone()[0]

    def cost_since(self, moment: datetime) -> float:
        with self._lock:
            return self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE started_at >= ?",
                (_iso(moment),),
            ).fetchone()[0]

    def queued(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE state = ?", (RUN_QUEUED,)
            ).fetchone()[0]

    def prune(self, older_than: timedelta) -> int:
        cutoff = _iso(self._clock() - older_than)
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM agent_runs WHERE created_at < ? AND state IN (?, ?)",
                (cutoff, RUN_DONE, RUN_FAILED),
            ).rowcount

    def _now(self) -> str:
        return _iso(self._clock())


def _trace(trajectory: Trajectory) -> list[dict]:
    steps = []
    for message in trajectory.messages:
        if isinstance(message, AgentTurn) and message.text:
            steps.append({"said": _cut(message.text)})
        elif isinstance(message, ToolResults):
            steps += [
                {
                    "tool": _cut(str(result.call.name)),
                    "args": _cut(json.dumps(result.call.args, ensure_ascii=False, default=str)),
                    "error": result.is_error,
                    "result": _cut(result.content),
                }
                for result in message.results
            ]
    return steps


def _cut(text: str) -> str:
    return text if len(text) <= TRACE_VALUE_CHARS else f"{text[:TRACE_VALUE_CHARS]}…"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _to_run(row: sqlite3.Row, **overrides: str) -> AgentRun:
    fields = {
        "id": row["id"],
        "trigger_key": row["trigger_key"],
        "kind": row["kind"],
        "payload": json.loads(row["payload"]),
        "state": row["state"],
        "created_at": datetime.fromisoformat(row["created_at"]),
        "outcome": row["outcome"],
        "answer": row["answer"],
    }
    return AgentRun(**{**fields, **overrides})
