import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.domain import (
    ACTION_CANCELLED,
    ACTION_DONE,
    ACTION_EXECUTING,
    ACTION_FAILED,
    ACTION_PENDING,
    PendingAction,
)

_COLUMNS = "id, kind, payload, payload_hash, chat_message_id, state, created_at, expires_at"
_SELECT = f"SELECT {_COLUMNS} FROM pending_actions"


class SqlitePendingActions:
    """Actions proposed in the chat; shares the decision store's connection and lock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock,
        clock: Callable[[], datetime],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._clock = clock

    def propose(self, kind: str, payload: dict, lifetime: timedelta) -> PendingAction:
        now = self._clock()
        encoded = _encode(payload)
        action = PendingAction(
            id=secrets.token_hex(8),
            kind=kind,
            payload=json.loads(encoded),
            state=ACTION_PENDING,
            created_at=now,
            expires_at=now + lifetime,
        )
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO pending_actions ({_COLUMNS}, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    action.id,
                    kind,
                    encoded,
                    _digest(encoded),
                    ACTION_PENDING,
                    _iso(now),
                    _iso(action.expires_at),
                    _iso(now),
                ),
            )
        return action

    def get(self, action_id: str) -> PendingAction | None:
        with self._lock:
            row = self._conn.execute(f"{_SELECT} WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else _to_action(row)

    def attach_chat_message(self, action_id: str, chat_message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE pending_actions SET chat_message_id = ?, updated_at = ? WHERE id = ?",
                (chat_message_id, _iso(self._clock()), action_id),
            )

    def begin(self, action_id: str, chat_message_id: int) -> PendingAction | None:
        return self._leave_pending(action_id, chat_message_id, ACTION_EXECUTING, live_only=True)

    def cancel(self, action_id: str, chat_message_id: int) -> bool:
        return (
            self._leave_pending(action_id, chat_message_id, ACTION_CANCELLED, live_only=False)
            is not None
        )

    def finish(self, action_id: str, succeeded: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE pending_actions SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (
                    ACTION_DONE if succeeded else ACTION_FAILED,
                    _iso(self._clock()),
                    action_id,
                    ACTION_EXECUTING,
                ),
            )

    def prune(self, older_than: timedelta) -> int:
        cutoff = _iso(self._clock() - older_than)
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM pending_actions WHERE created_at < ?", (cutoff,)
            ).rowcount

    def _leave_pending(
        self, action_id: str, chat_message_id: int, state: str, live_only: bool
    ) -> PendingAction | None:
        now = _iso(self._clock())
        query = (
            "UPDATE pending_actions SET state = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND chat_message_id = ?"
        )
        params: list[object] = [state, now, action_id, ACTION_PENDING, chat_message_id]
        if live_only:
            query += " AND expires_at > ?"
            params.append(now)
        with self._lock, self._conn:
            if self._conn.execute(query, params).rowcount != 1:
                return None
            row = self._conn.execute(f"{_SELECT} WHERE id = ?", (action_id,)).fetchone()
            if _digest(row["payload"]) != row["payload_hash"]:
                # What is about to be carried out is no longer what was shown and confirmed.
                self._conn.execute(
                    "UPDATE pending_actions SET state = ? WHERE id = ?", (ACTION_FAILED, action_id)
                )
                return None
        return _to_action(row)


def _encode(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        id=row["id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        chat_message_id=row["chat_message_id"],
    )
