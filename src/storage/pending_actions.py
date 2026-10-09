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
        # Normalised once: what is returned must equal what is read back, whatever the clock.
        now = self._clock().astimezone(UTC)
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
                    _digest(kind, encoded),
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

    def pending_on(self, chat_message_id: int) -> PendingAction | None:
        with self._lock:
            row = self._conn.execute(
                f"{_SELECT} WHERE chat_message_id = ? AND state = ? ORDER BY created_at DESC",
                (chat_message_id, ACTION_PENDING),
            ).fetchone()
        return None if row is None else _to_action(row)

    def attach_chat_message(self, action_id: str, chat_message_id: int) -> bool:
        with self._lock, self._conn:
            return (
                self._conn.execute(
                    "UPDATE pending_actions SET chat_message_id = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND chat_message_id IS NULL",
                    (chat_message_id, _iso(self._clock()), action_id, ACTION_PENDING),
                ).rowcount
                == 1
            )

    def begin(self, action_id: str, chat_message_id: int) -> PendingAction | None:
        now = _iso(self._clock())
        with self._lock, self._conn:
            taken = self._conn.execute(
                "UPDATE pending_actions SET state = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND chat_message_id = ? AND expires_at > ?",
                (ACTION_EXECUTING, now, action_id, ACTION_PENDING, chat_message_id, now),
            ).rowcount
            if taken != 1:
                return None
            row = self._conn.execute(f"{_SELECT} WHERE id = ?", (action_id,)).fetchone()
            if _digest(row["kind"], row["payload"]) != row["payload_hash"]:
                # What is about to be carried out is no longer what was shown and confirmed.
                self._conn.execute(
                    "UPDATE pending_actions SET state = ? WHERE id = ?", (ACTION_FAILED, action_id)
                )
                return None
        return _to_action(row)

    def cancel(self, action_id: str, chat_message_id: int) -> bool:
        with self._lock, self._conn:
            return (
                self._conn.execute(
                    "UPDATE pending_actions SET state = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND chat_message_id = ?",
                    (
                        ACTION_CANCELLED,
                        _iso(self._clock()),
                        action_id,
                        ACTION_PENDING,
                        chat_message_id,
                    ),
                ).rowcount
                == 1
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
                "DELETE FROM pending_actions WHERE expires_at < ?", (cutoff,)
            ).rowcount


def _encode(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _digest(kind: str, encoded: str) -> str:
    # The kind decides what is done with the payload: it is confirmed with it.
    return hashlib.sha256(f"{kind}\n{encoded}".encode()).hexdigest()


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
