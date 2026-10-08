import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.domain import CLOSED, IGNORED, FollowUpAnchor, TrackedThread

_COLUMNS = (
    "thread_id",
    "history_id",
    "state",
    "reason",
    "anchor_message_id",
    "anchor_sent_at",
    "anchor_to",
    "anchor_cc",
    "anchor_subject",
    "anchor_message_id_header",
    "anchor_references",
    "due_at",
    "expects_answer",
    "jev_asked_for",
    "proposal_state",
    "proposals_count",
    "snoozed_until",
    "proposal_text",
    "offered_on",
    "offered_at",
    "verdict",
    "updated_at",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM threads"
_REPLACE = (
    f"INSERT OR REPLACE INTO threads ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)


class SqliteThreadStore:
    """Threads holding a mail I sent; shares the decision store's connection and lock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock,
        clock: Callable[[], datetime],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._clock = clock

    def get(self, thread_id: str) -> TrackedThread | None:
        with self._lock:
            row = self._conn.execute(f"{_SELECT} WHERE thread_id = ?", (thread_id,)).fetchone()
        return None if row is None else _to_thread(row)

    def save(self, thread: TrackedThread) -> None:
        with self._lock, self._conn:
            self._conn.execute(_REPLACE, _to_row(thread))

    def update(
        self, thread_id: str, change: Callable[[TrackedThread | None], TrackedThread]
    ) -> TrackedThread:
        """Reads, changes and writes the thread in one transaction, so nothing written in
        between, such as a button pressed in the chat, is overwritten."""
        with self._lock, self._conn:
            row = self._conn.execute(f"{_SELECT} WHERE thread_id = ?", (thread_id,)).fetchone()
            thread = change(None if row is None else _to_thread(row))
            self._conn.execute(_REPLACE, _to_row(thread))
        return thread

    def in_state(self, state: str) -> list[TrackedThread]:
        with self._lock:
            rows = self._conn.execute(
                f"{_SELECT} WHERE state = ? ORDER BY due_at, thread_id", (state,)
            ).fetchall()
        return [_to_thread(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM threads GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def prune(self, older_than: timedelta) -> int:
        """Forgets settled threads; one still awaiting an answer is kept whatever its age."""
        cutoff = (self._clock() - older_than).isoformat()
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM threads WHERE state IN (?, ?) AND updated_at < ?",
                (CLOSED, IGNORED, cutoff),
            ).rowcount


def _to_row(thread: TrackedThread) -> tuple:
    anchor = thread.anchor
    return (
        thread.thread_id,
        thread.history_id,
        thread.state,
        thread.reason,
        anchor.message_id if anchor else None,
        _iso(anchor.sent_at) if anchor else None,
        json.dumps(list(anchor.to)) if anchor else None,
        json.dumps(list(anchor.cc)) if anchor else None,
        anchor.subject if anchor else None,
        anchor.message_id_header if anchor else None,
        json.dumps(list(anchor.references)) if anchor else None,
        _iso(thread.due_at),
        thread.expects_answer,
        thread.jev_asked_for,
        thread.proposal_state,
        thread.proposals_count,
        _iso(thread.snoozed_until),
        thread.proposal_text,
        thread.offered_on,
        _iso(thread.offered_at),
        thread.verdict,
        _iso(thread.updated_at),
    )


def _to_thread(row: sqlite3.Row) -> TrackedThread:
    anchor = None
    if row["anchor_message_id"] is not None:
        anchor = FollowUpAnchor(
            message_id=row["anchor_message_id"],
            sent_at=datetime.fromisoformat(row["anchor_sent_at"]),
            to=tuple(json.loads(row["anchor_to"])),
            cc=tuple(json.loads(row["anchor_cc"])),
            subject=row["anchor_subject"],
            message_id_header=row["anchor_message_id_header"],
            references=tuple(json.loads(row["anchor_references"])),
        )
    return TrackedThread(
        thread_id=row["thread_id"],
        history_id=row["history_id"],
        state=row["state"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        reason=row["reason"],
        anchor=anchor,
        due_at=_date(row["due_at"]),
        expects_answer=row["expects_answer"],
        jev_asked_for=row["jev_asked_for"],
        proposal_state=row["proposal_state"],
        proposals_count=row["proposals_count"],
        snoozed_until=_date(row["snoozed_until"]),
        proposal_text=row["proposal_text"],
        offered_on=row["offered_on"],
        offered_at=_date(row["offered_at"]),
        verdict=row["verdict"],
    )


def _iso(value: datetime | None) -> str | None:
    # UTC, so that ordering the text agrees with time across daylight saving changes.
    return None if value is None else value.astimezone(UTC).isoformat()


def _date(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
