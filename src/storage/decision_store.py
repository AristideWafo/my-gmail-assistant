import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.domain import (
    FEEDBACK_ORIGINS,
    VERDICTS,
    Correction,
    DecisionRecord,
    EmailMessage,
    FeedbackTally,
    RatedDecision,
    RuleCandidate,
    TriageResult,
)
from src.errors import BackupError
from src.storage.migrations import LATEST_VERSION, migrate, needs_safety_copy
from src.storage.thread_store import SqliteThreadStore

EXCERPT_CHARS = 300

_MEMORY = ":memory:"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
PRUNABLE_STATE_PREFIXES = (
    "reply:",
    "draft_sent:",
    "bot_draft:",
    "unsub:",
    "unsub_done:",
    "cmd:",
    "llm_spend:",
    "proactive:",
)

_DECISION_COLUMNS = (
    "message_id, thread_id, sender, subject, excerpt, urgency, category, confidence, route, "
    "created_at, chat_message_id, message_id_header, source, needs_reply, signals, put_forward"
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
        self.threads = SqliteThreadStore(self._conn, self._lock, clock)

    def record_decision(
        self, email: EmailMessage, triage: TriageResult, route: str, put_forward: bool = False
    ) -> None:
        excerpt = (email.body or email.snippet or "")[:EXCERPT_CHARS]
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO decisions ({_DECISION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                "thread_id = excluded.thread_id, sender = excluded.sender, "
                "subject = excluded.subject, excerpt = excluded.excerpt, "
                "urgency = excluded.urgency, category = excluded.category, "
                "confidence = excluded.confidence, route = excluded.route, "
                "message_id_header = excluded.message_id_header, source = excluded.source, "
                "needs_reply = excluded.needs_reply, signals = excluded.signals, "
                "put_forward = excluded.put_forward",
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
                    triage.needs_reply,
                    json.dumps(triage.signals, sort_keys=True) if triage.signals else None,
                    int(put_forward),
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

    def record_feedback(self, message_id: str, verdict: str, origin: str = "alert") -> bool:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}, expected one of {VERDICTS}")
        if origin not in FEEDBACK_ORIGINS:
            raise ValueError(f"unknown origin {origin!r}, expected one of {FEEDBACK_ORIGINS}")
        with self._lock, self._conn:
            known = self._conn.execute(
                "SELECT 1 FROM decisions WHERE message_id = ?", (message_id,)
            ).fetchone()
            if known is None:
                return False
            # REPLACE re-inserts with a fresh AUTOINCREMENT id, so the latest verdict sorts newest
            # even when the clock yields identical timestamps.
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback (message_id, verdict, created_at, origin) "
                "VALUES (?, ?, ?, ?)",
                (message_id, verdict, self._now(), origin),
            )
        return True

    def recent_corrections(
        self, limit: int, verdicts: tuple[str, ...] | None = None
    ) -> list[Correction]:
        # SQLite treats a negative LIMIT as unbounded.
        if limit <= 0:
            return []
        if verdicts is None:
            verdicts = tuple(verdict for verdict in VERDICTS if verdict != "valid")
        placeholders = ", ".join("?" for _ in verdicts)
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.sender, d.subject, d.excerpt, d.urgency, d.category, f.verdict "
                "FROM feedback f JOIN decisions d ON d.message_id = f.message_id "
                f"WHERE f.verdict IN ({placeholders}) ORDER BY f.id DESC LIMIT ?",
                (*verdicts, limit),
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

    def review_candidates(self, max_age: timedelta, limit: int) -> list[DecisionRecord]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                # Rule decisions are deterministic and urgent ones already carry feedback
                # buttons: only what the classifier decided silently is worth a second look.
                f"SELECT {_DECISION_COLUMNS} FROM decisions "
                "WHERE created_at >= ? AND route IN ('reject', 'label') AND source != 'rule' "
                "AND message_id NOT IN (SELECT message_id FROM feedback) "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (self._cutoff(max_age), limit),
            ).fetchall()
        return [_to_record(row) for row in rows]

    def decisions_since(self, max_age: timedelta) -> list[DecisionRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE created_at >= ? "
                "ORDER BY created_at, rowid",
                (self._cutoff(max_age),),
            ).fetchall()
        return [_to_record(row) for row in rows]

    def put_forward_pending(self, max_age: timedelta) -> list[DecisionRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_DECISION_COLUMNS} FROM decisions "
                "WHERE put_forward = 1 AND created_at >= ? "
                "AND message_id NOT IN (SELECT message_id FROM feedback) "
                "ORDER BY created_at, rowid",
                (self._cutoff(max_age),),
            ).fetchall()
        return [_to_record(row) for row in rows]

    def feedback_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM feedback GROUP BY verdict"
            ).fetchall()
        counts = dict.fromkeys(VERDICTS, 0)
        counts.update({row["verdict"]: row["n"] for row in rows})
        return counts

    def feedback_breakdown(self) -> list[FeedbackTally]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.route, d.source, f.verdict, COUNT(*) AS n "
                "FROM feedback f JOIN decisions d ON d.message_id = f.message_id "
                "GROUP BY d.route, d.source, f.verdict ORDER BY d.route, d.source, f.verdict"
            ).fetchall()
        return [FeedbackTally(row["route"], row["source"], row["verdict"], row["n"]) for row in rows]

    def decision_counts_by_source(self, max_age: timedelta) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM decisions WHERE created_at >= ? "
                "GROUP BY source ORDER BY n DESC, source",
                (self._cutoff(max_age),),
            ).fetchall()
        return {row["source"]: row["n"] for row in rows}

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
                record=_to_record(row),
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
                "SUM(f.verdict IN ('wrong_archive', 'missed_urgent', 'missed_important')) "
                "AS wanted_back "
                "FROM decisions d LEFT JOIN feedback f ON f.message_id = d.message_id "
                "WHERE d.sender = ? COLLATE NOCASE AND d.created_at >= ?",
                (sender, self._cutoff(max_age)),
            ).fetchone()
        if not row["total"] or row["archived"] != row["total"] or row["wanted_back"]:
            return 0
        return row["archived"]

    def prune(self, older_than: timedelta) -> int:
        """Deletes expired dedup state, unrated decisions and settled threads; feedback is kept."""
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
        return deleted + self.threads.prune(older_than)

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
        return None if row is None else _to_record(row)

    def _now(self) -> str:
        return self._clock().isoformat()

    def _cutoff(self, age: timedelta) -> str:
        return (self._clock() - age).isoformat()


def _to_record(row: sqlite3.Row) -> DecisionRecord:
    fields = {name: row[name] for name in _DECISION_FIELDS}
    fields["signals"] = _decode_signals(fields["signals"])
    fields["put_forward"] = bool(fields["put_forward"])
    return DecisionRecord(**fields)


def _decode_signals(raw: str | None) -> dict[str, float]:
    # Diagnostic data: an unreadable value must not make the whole decision unreadable.
    try:
        decoded = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
