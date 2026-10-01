import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.domain import (
    VERDICTS,
    Correction,
    DecisionRecord,
    EmailMessage,
    RatedDecision,
    RuleCandidate,
    TriageResult,
)
from src.errors import BackupError
from src.storage.migrations import LATEST_VERSION, migrate, needs_safety_copy

EXCERPT_CHARS = 300

_MEMORY = ":memory:"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
PRUNABLE_STATE_PREFIXES = ("reply:", "draft_sent:", "bot_draft:", "unsub:", "unsub_done:")

_DECISION_COLUMNS = (
    "message_id, thread_id, sender, subject, excerpt, urgency, category, confidence, route, "
    "created_at, chat_message_id, message_id_header, source"
)
_DECISION_FIELDS = tuple(name.strip() for name in _DECISION_COLUMNS.split(","))
_PREFIXED_DECISION_COLUMNS = ", ".join(f"d.{name}" for name in _DECISION_FIELDS)


class SqliteDecisionStore:
    def __init__(
        self, path: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        if path != _MEMORY:
            _create_private_file(path)
        # Shared by the polling thread and the Telegram listener; self._lock serializes access.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path != _MEMORY:
            self._conn.execute("PRAGMA journal_mode=WAL")
            if needs_safety_copy(self._conn):
                # A migration rewrites the only copy of the verdicts: keep the previous state.
                self.backup(f"{path}.pre-v{LATEST_VERSION}")
        migrate(self._conn)

    def record_decision(self, email: EmailMessage, triage: TriageResult, route: str) -> None:
        excerpt = (email.body or email.snippet or "")[:EXCERPT_CHARS]
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO decisions ({_DECISION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                "thread_id = excluded.thread_id, sender = excluded.sender, "
                "subject = excluded.subject, excerpt = excluded.excerpt, "
                "urgency = excluded.urgency, category = excluded.category, "
                "confidence = excluded.confidence, route = excluded.route, "
                "message_id_header = excluded.message_id_header, source = excluded.source",
                (
                    email.id,
                    email.thread_id,
                    email.sender,
                    email.subject,
                    excerpt,
                    triage.urgency,
                    triage.category,
                    float(triage.confidence),
                    route,
                    self._now(),
                    getattr(email, "message_id_header", "") or "",
                    triage.source,
                ),
            )

    def get(self, message_id: str) -> DecisionRecord | None:
        return self._fetch_decision("message_id = ?", message_id)

    def attach_chat_message(self, message_id: str, chat_message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE decisions SET chat_message_id = ? WHERE message_id = ?",
                (chat_message_id, message_id),
            )

    def find_by_chat_message(self, chat_message_id: int) -> DecisionRecord | None:
        # Telegram message ids are only unique per chat: after a TELEGRAM_CHAT_ID change an old
        # alert can share the id, and the reply is meant for the newest one.
        return self._fetch_decision(
            "chat_message_id = ?", chat_message_id, order_by="created_at DESC, rowid DESC"
        )

    def record_feedback(self, message_id: str, verdict: str) -> bool:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}, expected one of {VERDICTS}")
        with self._lock, self._conn:
            known = self._conn.execute(
                "SELECT 1 FROM decisions WHERE message_id = ?", (message_id,)
            ).fetchone()
            if known is None:
                return False
            # REPLACE re-inserts with a fresh AUTOINCREMENT id, so the latest verdict sorts newest
            # even when the clock yields identical timestamps.
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback (message_id, verdict, created_at) "
                "VALUES (?, ?, ?)",
                (message_id, verdict, self._now()),
            )
        return True

    def recent_corrections(self, limit: int) -> list[Correction]:
        # SQLite treats a negative LIMIT as unbounded.
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.sender, d.subject, d.excerpt, d.urgency, d.category, f.verdict "
                "FROM feedback f JOIN decisions d ON d.message_id = f.message_id "
                "WHERE f.verdict != 'valid' ORDER BY f.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Correction(
                sender=row["sender"],
                subject=row["subject"],
                excerpt=row["excerpt"],
                predicted_urgency=row["urgency"],
                predicted_category=row["category"],
                verdict=row["verdict"],
            )
            for row in rows
        ]

    def feedback_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM feedback GROUP BY verdict"
            ).fetchall()
        counts = dict.fromkeys(VERDICTS, 0)
        counts.update({row["verdict"]: row["n"] for row in rows})
        return counts

    def mark_alerted(self, message_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                # A re-alert must refresh the timestamp or the time window would keep expiring.
                "INSERT INTO alerted (message_id, alerted_at) VALUES (?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET alerted_at = excluded.alerted_at",
                (message_id, self._now()),
            )

    def was_alerted(self, message_id: str, within_seconds: float | None = None) -> bool:
        query, params = "SELECT 1 FROM alerted WHERE message_id = ?", [message_id]
        if within_seconds is not None:
            query += " AND alerted_at >= ?"
            params.append(self._cutoff(timedelta(seconds=within_seconds)))
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return row is not None

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, value, self._now()),
            )

    def rated_decisions(self) -> list[RatedDecision]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_PREFIXED_DECISION_COLUMNS}, f.verdict, f.created_at AS rated_at "
                "FROM feedback f JOIN decisions d ON d.message_id = f.message_id ORDER BY f.id"
            ).fetchall()
        return [
            RatedDecision(
                record=DecisionRecord(**{name: row[name] for name in _DECISION_FIELDS}),
                verdict=row["verdict"],
                rated_at=row["rated_at"],
            )
            for row in rows
        ]

    def rule_candidates(self, min_count: int, max_age: timedelta) -> list[RuleCandidate]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sender, MIN(urgency) AS urgency, MIN(category) AS category, "
                "COUNT(*) AS n FROM decisions "
                "WHERE source = 'jev' AND created_at >= ? AND sender NOT IN ("
                "  SELECT d.sender FROM feedback f JOIN decisions d "
                "  ON d.message_id = f.message_id WHERE f.verdict != 'valid') "
                "GROUP BY sender "
                "HAVING n >= ? AND COUNT(DISTINCT urgency || '/' || category) = 1 "
                "ORDER BY n DESC, sender",
                (self._cutoff(max_age), min_count),
            ).fetchall()
        return [
            RuleCandidate(row["sender"], row["urgency"], row["category"], row["n"])
            for row in rows
        ]

    def archived_streak(self, sender: str, max_age: timedelta) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(d.route = 'reject') AS archived, "
                "SUM(f.verdict = 'wrong_archive') AS wanted_back "
                "FROM decisions d LEFT JOIN feedback f ON f.message_id = d.message_id "
                "WHERE d.sender = ? COLLATE NOCASE AND d.created_at >= ?",
                (sender, self._cutoff(max_age)),
            ).fetchone()
        if not row["total"] or row["archived"] != row["total"] or row["wanted_back"]:
            return 0
        return row["archived"]

    def prune(self, older_than: timedelta) -> int:
        """Deletes expired dedup state and unrated decisions; feedback and other state are kept."""
        cutoff = self._cutoff(older_than)
        # GLOB, not LIKE: "_" in the prefixes is a LIKE wildcard.
        prefixes = " OR ".join("key GLOB ?" for _ in PRUNABLE_STATE_PREFIXES)
        with self._lock, self._conn:
            deleted = self._conn.execute(
                "DELETE FROM decisions WHERE created_at < ? AND message_id NOT IN "
                "(SELECT message_id FROM feedback)",
                (cutoff,),
            ).rowcount
            deleted += self._conn.execute(
                "DELETE FROM alerted WHERE alerted_at < ?", (cutoff,)
            ).rowcount
            deleted += self._conn.execute(
                f"DELETE FROM kv_state WHERE updated_at < ? AND ({prefixes})",
                (cutoff, *(f"{prefix}*" for prefix in PRUNABLE_STATE_PREFIXES)),
            ).rowcount
        return deleted

    def backup(self, destination: str) -> None:
        _create_private_file(destination)
        target = sqlite3.connect(destination)
        try:
            with self._lock:
                self._conn.backup(target)
            # A copy of a damaged database must fail here, not be trusted on restore day.
            verdict = _integrity_verdict(target)
        finally:
            target.close()
        if verdict != "ok":
            raise BackupError(f"backup failed its integrity check: {verdict}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _fetch_decision(
        self, where: str, value: object, order_by: str = "rowid"
    ) -> DecisionRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE {where} "
                f"ORDER BY {order_by} LIMIT 1",
                (value,),
            ).fetchone()
        return None if row is None else DecisionRecord(**dict(row))

    def _now(self) -> str:
        return self._clock().isoformat()

    def _cutoff(self, age: timedelta) -> str:
        return (self._clock() - age).isoformat()


def _integrity_verdict(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _create_private_file(path: str) -> None:
    # The store holds mail subjects, senders and excerpts: keep it owner-only. Pre-existing
    # directories (e.g. an image-owned /data) are left untouched; SQLite gives the -wal and
    # -shm files the permissions of the database file.
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
        os.chmod(directory, _DIR_MODE)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
    except FileExistsError:
        return
    os.close(fd)
    os.chmod(path, _FILE_MODE)
