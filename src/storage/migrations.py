import sqlite3

from src.errors import SchemaVersionError

# Append only: a database remembers how many of these it has applied (PRAGMA user_version), so
# editing a released script would leave existing databases on a different schema than new ones.
MIGRATIONS: tuple[str, ...] = (
    # IF NOT EXISTS: databases created before versioning already hold these tables at version 0.
    """
    CREATE TABLE IF NOT EXISTS decisions (
        message_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        sender TEXT NOT NULL,
        subject TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        urgency TEXT NOT NULL,
        category TEXT NOT NULL,
        confidence REAL NOT NULL,
        route TEXT NOT NULL,
        created_at TEXT NOT NULL,
        chat_message_id INTEGER,
        message_id_header TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_decisions_chat_message_id ON decisions (chat_message_id);
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL UNIQUE,
        verdict TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS alerted (
        message_id TEXT PRIMARY KEY,
        alerted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS kv_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE decisions ADD COLUMN source TEXT NOT NULL DEFAULT '';
    ALTER TABLE feedback ADD COLUMN origin TEXT NOT NULL DEFAULT 'alert';
    """,
    # NULL means the question was not asked, which a default of 0 would report as "no".
    "ALTER TABLE decisions ADD COLUMN needs_reply REAL;",
    # Until this version /review had one button, "Urgent raté", for every mail that deserved
    # more than a label. Its user meant "put it forward", not "ring": kept as missed_urgent,
    # those verdicts would go on teaching the classifier to alert on such mails.
    """
    UPDATE feedback SET verdict = 'missed_important'
    WHERE verdict = 'missed_urgent' AND origin = 'review';
    """,
    # JSON object, question name to probability; NULL when no attention question was answered.
    "ALTER TABLE decisions ADD COLUMN signals TEXT;",
    "ALTER TABLE decisions ADD COLUMN put_forward INTEGER NOT NULL DEFAULT 0;",
    # One row per thread holding a mail I sent; recipients and references are JSON arrays.
    """
    CREATE TABLE threads (
        thread_id TEXT PRIMARY KEY,
        history_id TEXT NOT NULL,
        state TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        anchor_message_id TEXT,
        anchor_sent_at TEXT,
        anchor_to TEXT,
        anchor_cc TEXT,
        anchor_subject TEXT,
        anchor_message_id_header TEXT,
        anchor_references TEXT,
        due_at TEXT,
        expects_answer REAL,
        jev_asked_for TEXT NOT NULL DEFAULT '',
        proposal_state TEXT NOT NULL DEFAULT 'none',
        proposals_count INTEGER NOT NULL DEFAULT 0,
        snoozed_until TEXT,
        proposal_text TEXT NOT NULL DEFAULT '',
        offered_on INTEGER,
        offered_at TEXT,
        verdict TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_threads_state ON threads (state);
    """,
    # One row per action proposed in the chat and awaiting a button; payload is a JSON object.
    """
    CREATE TABLE pending_actions (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        chat_message_id INTEGER,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # One row per thing asked of the agent; the unique trigger key is what makes a redelivered
    # chat message start a single run. payload and trace are JSON.
    """
    CREATE TABLE agent_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        state TEXT NOT NULL,
        outcome TEXT NOT NULL DEFAULT '',
        answer TEXT NOT NULL DEFAULT '',
        trace TEXT NOT NULL DEFAULT '[]',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE INDEX idx_agent_runs_state ON agent_runs (state);
    """,
    # What the user asked to be remembered about a correspondent; scope is a canonical address.
    """
    CREATE TABLE memory_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_memory_notes_scope ON memory_notes (scope);
    """,
    # A follow-up the agent wrote for a thread; composed_for is the anchor it was written for.
    """
    ALTER TABLE threads ADD COLUMN composed_text TEXT NOT NULL DEFAULT '';
    ALTER TABLE threads ADD COLUMN composed_for TEXT NOT NULL DEFAULT '';
    ALTER TABLE threads ADD COLUMN composed_advice TEXT NOT NULL DEFAULT '';
    """,
)
LATEST_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def needs_safety_copy(conn: sqlite3.Connection) -> bool:
    """True when a migration is about to rewrite a database that already holds tables."""
    if schema_version(conn) >= LATEST_VERSION:
        return False
    return conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > LATEST_VERSION:
        raise SchemaVersionError(
            f"database is at schema version {current}, this build only knows up to "
            f"{LATEST_VERSION}; run a newer build or restore an older backup"
        )
    for version in range(current + 1, LATEST_VERSION + 1):
        try:
            # One transaction per step, version included: a crash leaves the step fully applied
            # or not at all, never half a schema with a stale version.
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version - 1]}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except sqlite3.Error:
            conn.rollback()
            raise
