import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.domain import CLOSED, IGNORED, WAITING_FOR_THEM, FollowUpAnchor, TrackedThread
from src.storage import SqliteDecisionStore
from src.storage.migrations import LATEST_VERSION, MIGRATIONS, schema_version

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
ANCHOR = FollowUpAnchor(
    message_id="m1",
    sent_at=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
    to=("jean@example.com",),
    cc=("paul@example.com",),
    subject="Devis",
    message_id_header="<m1@x>",
    references=("<m0@x>", "<m1@x>"),
)


def thread(thread_id="t1", state=WAITING_FOR_THEM, **fields):
    return TrackedThread(
        thread_id=thread_id, history_id="7", state=state, updated_at=NOW, anchor=ANCHOR, **fields
    )


class ThreadStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.threads = self.store.threads

    def test_every_field_survives_a_round_trip(self):
        full = thread(
            reason="",
            due_at=NOW,
            expects_answer=0.82,
            jev_asked_for="m1",
            proposal_state="offered",
            proposals_count=1,
            snoozed_until=NOW + timedelta(days=3),
            proposal_text="Bonjour,\n\nJe reviens vers vous.",
            offered_on=42,
            offered_at=NOW,
            verdict="useful",
        )
        self.threads.save(full)

        self.assertEqual(self.threads.get("t1"), full)

    def test_a_thread_without_anchor_and_optional_dates(self):
        bare = TrackedThread("t2", "3", IGNORED, NOW, reason="not_mine")
        self.threads.save(bare)

        self.assertEqual(self.threads.get("t2"), bare)
        self.assertIsNone(self.threads.get("unknown"))

    def test_saving_again_replaces_the_row(self):
        self.threads.save(thread())
        self.threads.save(replace(thread(), state=CLOSED, reason="answered"))

        self.assertEqual(self.threads.get("t1").state, CLOSED)
        self.assertEqual(self.threads.counts(), {CLOSED: 1})

    def test_update_changes_the_row_as_currently_stored(self):
        self.threads.save(thread(proposal_state="dismissed"))

        updated = self.threads.update("t1", lambda t: replace(t, history_id="8"))

        self.assertEqual(updated.proposal_state, "dismissed")
        self.assertEqual(self.threads.get("t1"), updated)

    def test_update_can_create_a_thread(self):
        created = self.threads.update("t2", lambda t: thread("t2") if t is None else t)

        self.assertEqual(self.threads.get("t2"), created)

    def test_threads_in_a_state_come_earliest_due_first(self):
        self.threads.save(thread("late", due_at=NOW + timedelta(days=2)))
        self.threads.save(thread("soon", due_at=NOW))
        self.threads.save(thread("done", state=CLOSED))

        self.assertEqual([t.thread_id for t in self.threads.in_state(WAITING_FOR_THEM)], ["soon", "late"])
        self.assertEqual(self.threads.counts(), {WAITING_FOR_THEM: 2, CLOSED: 1})

    def test_pruning_forgets_old_settled_threads_only(self):
        self.threads.save(thread("waiting"))
        self.threads.save(thread("closed", state=CLOSED))
        self.threads.save(thread("ignored", state=IGNORED))
        self.clock[0] = NOW + timedelta(days=200)
        self.threads.save(replace(thread("recent", state=CLOSED), updated_at=self.clock[0]))

        deleted = self.store.prune(timedelta(days=90))

        self.assertEqual(deleted, 2)
        remaining = {t.thread_id for state in (WAITING_FOR_THEM, CLOSED) for t in self.threads.in_state(state)}
        self.assertEqual(remaining, {"waiting", "recent"})


class ThreadsMigrationTests(unittest.TestCase):
    def test_version_6_database_gains_an_empty_threads_table_and_keeps_its_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "assistant.db")
        conn = sqlite3.connect(path)
        conn.executescript("".join(MIGRATIONS[:6]) + "PRAGMA user_version = 6;")
        conn.execute(
            "INSERT INTO decisions (message_id, thread_id, sender, subject, excerpt, urgency, "
            "category, confidence, route, created_at) "
            "VALUES ('m1', 't1', 'a@b.com', 'Hi', 'b', 'low', 'personnel', 0.9, 'label', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        store = SqliteDecisionStore(path)
        self.addCleanup(store.close)

        self.assertEqual(schema_version(store._conn), LATEST_VERSION)
        self.assertIsNotNone(store.get("m1"))
        self.assertEqual(store.threads.counts(), {})
        self.assertTrue(os.path.exists(f"{path}.pre-v{LATEST_VERSION}"))


if __name__ == "__main__":
    unittest.main()
