import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from src.domain import MemoryNote

MAX_NOTES_PER_SCOPE = 20
_SELECT = "SELECT id, scope, text, created_at FROM memory_notes"


class SqliteMemoryNotes:
    """What the user asked to be remembered; shares the decision store's connection and lock.
    Never pruned: only the user forgets a note."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock,
        clock: Callable[[], datetime],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._clock = clock

    def add(self, scope: str, text: str) -> MemoryNote | None:
        now = self._clock().astimezone(UTC)
        with self._lock, self._conn:
            held = self._conn.execute(
                "SELECT COUNT(*) FROM memory_notes WHERE scope = ?", (scope,)
            ).fetchone()[0]
            if held >= MAX_NOTES_PER_SCOPE:
                return None
            note_id = self._conn.execute(
                "INSERT INTO memory_notes (scope, text, created_at) VALUES (?, ?, ?)",
                (scope, text, now.isoformat()),
            ).lastrowid
        return MemoryNote(note_id, scope, text, now)

    def about(self, scope: str) -> list[MemoryNote]:
        with self._lock:
            rows = self._conn.execute(f"{_SELECT} WHERE scope = ? ORDER BY id", (scope,)).fetchall()
        return [_to_note(row) for row in rows]

    def everything(self) -> list[MemoryNote]:
        with self._lock:
            rows = self._conn.execute(f"{_SELECT} ORDER BY scope, id").fetchall()
        return [_to_note(row) for row in rows]

    def forget(self, note_id: int) -> bool:
        with self._lock, self._conn:
            return (
                self._conn.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,)).rowcount
                == 1
            )


def _to_note(row: sqlite3.Row) -> MemoryNote:
    return MemoryNote(
        row["id"], row["scope"], row["text"], datetime.fromisoformat(row["created_at"])
    )
