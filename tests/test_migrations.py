import os
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from src.domain import EmailMessage, TriageResult
from src.errors import SchemaVersionError
from src.storage import SqliteDecisionStore, migrations
from src.storage.migrations import LATEST_VERSION, MIGRATIONS, migrate, schema_version


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def make_email(message_id="m1"):
    return EmailMessage(
        id=message_id, thread_id="t1", sender="a@b.com", subject="Hi", snippet="s", body="b"
    )


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_fresh_database_reaches_the_latest_version(self):
        migrate(self.conn)

        self.assertEqual(schema_version(self.conn), LATEST_VERSION)
        self.assertIn("source", columns(self.conn, "decisions"))
        self.assertIn("origin", columns(self.conn, "feedback"))

    def test_running_again_changes_nothing(self):
        migrate(self.conn)
        migrate(self.conn)

        self.assertEqual(schema_version(self.conn), LATEST_VERSION)

    def test_database_from_a_newer_build_is_refused(self):
        self.conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")

        with self.assertRaisesRegex(SchemaVersionError, "newer build"):
            migrate(self.conn)

    def test_a_failing_step_is_rolled_back_with_its_version(self):
        broken = (*MIGRATIONS, "CREATE TABLE half_done (id INTEGER); ALTER TABLE nope ADD x TEXT;")

        with (
            patch.object(migrations, "MIGRATIONS", broken),
            patch.object(migrations, "LATEST_VERSION", len(broken)),
            self.assertRaises(sqlite3.Error),
        ):
            migrate(self.conn)

        self.assertEqual(schema_version(self.conn), LATEST_VERSION)
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("half_done", tables)


class StoreMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "assistant.db")
        self.copy = f"{self.path}.pre-v{LATEST_VERSION}"

    def make_unversioned_database(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(MIGRATIONS[0])
        conn.execute(
            "INSERT INTO decisions (message_id, thread_id, sender, subject, excerpt, urgency, "
            "category, confidence, route, created_at) "
            "VALUES ('m1', 't1', 'a@b.com', 'Hi', 'b', 'high', 'personnel', 0.9, 'llm', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO feedback (message_id, verdict, created_at) "
            "VALUES ('m1', 'false_urgent', '2026-01-02')"
        )
        conn.commit()
        conn.close()

    def open_store(self):
        store = SqliteDecisionStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_unversioned_database_is_upgraded_keeping_its_rows(self):
        self.make_unversioned_database()

        store = self.open_store()

        self.assertEqual(schema_version(store._conn), LATEST_VERSION)
        self.assertEqual(store.get("m1").source, "")
        self.assertEqual(store.feedback_counts()["false_urgent"], 1)
        origin = store._conn.execute("SELECT origin FROM feedback").fetchone()[0]
        self.assertEqual(origin, "alert")

    def test_upgrade_leaves_a_private_copy_of_the_previous_state(self):
        self.make_unversioned_database()

        self.open_store()

        self.assertEqual(stat.S_IMODE(os.stat(self.copy).st_mode), 0o600)
        previous = sqlite3.connect(self.copy)
        self.addCleanup(previous.close)
        self.assertEqual(schema_version(previous), 0)
        self.assertNotIn("source", columns(previous, "decisions"))
        self.assertEqual(previous.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 1)

    def test_new_or_up_to_date_database_gets_no_copy(self):
        SqliteDecisionStore(self.path).close()
        SqliteDecisionStore(self.path).close()

        self.assertFalse(os.path.exists(self.copy))

    def test_database_from_a_newer_build_stops_startup(self):
        SqliteDecisionStore(self.path).close()
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
        conn.close()

        with self.assertRaises(SchemaVersionError):
            SqliteDecisionStore(self.path)


class LegacyMissedUrgentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "assistant.db")

    def make_version_3_database(self, feedback: list[tuple[str, str, str]]):
        conn = sqlite3.connect(self.path)
        conn.executescript("".join(MIGRATIONS[:3]) + "PRAGMA user_version = 3;")
        for message_id, verdict, origin in feedback:
            conn.execute(
                "INSERT INTO decisions (message_id, thread_id, sender, subject, excerpt, "
                "urgency, category, confidence, route, created_at, source) "
                "VALUES (?, 't', 'a@b.com', 'Hi', 'b', 'low', 'personnel', 0.9, 'label', "
                "'2026-01-01', 'jev')",
                (message_id,),
            )
            conn.execute(
                "INSERT INTO feedback (message_id, verdict, created_at, origin) "
                "VALUES (?, ?, '2026-01-02', ?)",
                (message_id, verdict, origin),
            )
        conn.commit()
        conn.close()

    def test_review_verdicts_given_with_the_single_button_become_missed_important(self):
        self.make_version_3_database(
            [
                ("kept", "missed_urgent", "review"),
                ("fine", "valid", "review"),
                ("archived", "wrong_archive", "review"),
                ("alerted", "false_urgent", "alert"),
            ]
        )

        store = SqliteDecisionStore(self.path)
        self.addCleanup(store.close)

        verdicts = {rated.record.message_id: rated.verdict for rated in store.rated_decisions()}
        self.assertEqual(
            verdicts,
            {
                "kept": "missed_important",
                "fine": "valid",
                "archived": "wrong_archive",
                "alerted": "false_urgent",
            },
        )
        self.assertEqual(store.recent_corrections(8, ("missed_urgent",)), [])

    def test_the_previous_verdicts_stay_in_the_safety_copy(self):
        self.make_version_3_database([("kept", "missed_urgent", "review")])

        SqliteDecisionStore(self.path).close()

        previous = sqlite3.connect(f"{self.path}.pre-v{LATEST_VERSION}")
        self.addCleanup(previous.close)
        self.assertEqual(
            previous.execute("SELECT verdict FROM feedback").fetchone()[0], "missed_urgent"
        )

    def test_a_verdict_given_after_the_upgrade_keeps_its_meaning(self):
        store = SqliteDecisionStore(self.path)
        self.addCleanup(store.close)
        store.record_decision(make_email(), TriageResult("low", "personnel", 0.9, "jev"), "label")

        store.record_feedback("m1", "missed_urgent", origin="review")

        self.assertEqual(store.feedback_counts()["missed_urgent"], 1)


class DecisionSourceTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)

    def test_source_is_stored_and_follows_a_reclassification(self):
        self.store.record_decision(make_email(), TriageResult("low", "newsletter", 1.0, "rule"), "reject")
        self.assertEqual(self.store.get("m1").source, "rule")

        self.store.record_decision(make_email(), TriageResult("high", "personnel", 0.8, "jev"), "llm")
        self.assertEqual(self.store.get("m1").source, "jev")

    def test_source_defaults_to_empty(self):
        self.store.record_decision(make_email(), TriageResult("low", "personnel", 0.6), "label")

        self.assertEqual(self.store.get("m1").source, "")


class NeedsReplyColumnTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "assistant.db")

    def open_store(self):
        store = SqliteDecisionStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_version_2_database_is_upgraded_keeping_its_rows(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(MIGRATIONS[0] + MIGRATIONS[1] + "PRAGMA user_version = 2;")
        conn.execute(
            "INSERT INTO decisions (message_id, thread_id, sender, subject, excerpt, urgency, "
            "category, confidence, route, created_at, source) "
            "VALUES ('m1', 't1', 'a@b.com', 'Hi', 'b', 'low', 'personnel', 0.9, 'label', "
            "'2026-01-01', 'jev')"
        )
        conn.commit()
        conn.close()

        store = self.open_store()

        self.assertEqual(schema_version(store._conn), LATEST_VERSION)
        record = store.get("m1")
        self.assertEqual((record.source, record.route), ("jev", "label"))
        self.assertIsNone(record.needs_reply)
        self.assertTrue(os.path.exists(f"{self.path}.pre-v{LATEST_VERSION}"))

    def test_probability_is_stored_and_follows_a_reclassification(self):
        store = self.open_store()

        store.record_decision(make_email(), TriageResult("low", "personnel", 0.9, "jev", 0.93), "label")
        self.assertEqual(store.get("m1").needs_reply, 0.93)

        store.record_decision(make_email(), TriageResult("low", "personnel", 0.9, "heuristic"), "label")
        self.assertIsNone(store.get("m1").needs_reply)

    def test_rated_decisions_carry_the_probability(self):
        store = self.open_store()
        store.record_decision(make_email(), TriageResult("low", "personnel", 0.9, "jev", 0.2), "label")
        store.record_feedback("m1", "valid")

        self.assertEqual(store.rated_decisions()[0].record.needs_reply, 0.2)


if __name__ == "__main__":
    unittest.main()
