import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.domain import EmailMessage, TriageResult
from src.errors import BackupError
from src.maintenance import BackupRotation
from src.storage import SqliteDecisionStore


def make_email(message_id="m1"):
    return EmailMessage(
        id=message_id,
        thread_id=f"t-{message_id}",
        sender="alice@example.com",
        subject="subject",
        snippet="snippet",
        body="body",
    )


class BackupRotationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / "backups"
        self.now = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)

    def rotation(self, keep=7, store=None):
        return BackupRotation(
            store or self.store, str(self.directory), keep, clock=lambda: self.now
        )

    def names(self):
        return sorted(path.name for path in self.directory.iterdir())

    def test_writes_one_private_dated_copy_and_nothing_else(self):
        path = self.rotation().run()

        self.assertEqual(path, self.directory / "assistant-2026-10-01.db")
        self.assertEqual(self.names(), ["assistant-2026-10-01.db"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_backup_restores_decisions_verdicts_and_state(self):
        self.store.record_decision(make_email(), TriageResult("high", "personnel", 0.9), "llm")
        self.store.record_feedback("m1", "false_urgent")
        self.store.set_state("telegram_offset", "42")

        restored = SqliteDecisionStore(str(self.rotation().run()))
        self.addCleanup(restored.close)

        self.assertEqual(restored.get("m1").subject, "subject")
        self.assertEqual(restored.feedback_counts()["false_urgent"], 1)
        self.assertEqual(restored.get_state("telegram_offset"), "42")

    def test_a_second_run_the_same_day_replaces_the_copy(self):
        self.rotation().run()
        self.store.set_state("key", "later")

        path = self.rotation().run()

        self.assertEqual(self.names(), ["assistant-2026-10-01.db"])
        restored = SqliteDecisionStore(str(path))
        self.addCleanup(restored.close)
        self.assertEqual(restored.get_state("key"), "later")

    def test_keeps_only_the_most_recent_copies_and_leaves_other_files_alone(self):
        self.directory.mkdir()
        (self.directory / "notes.txt").write_text("mine")
        (self.directory / "assistant-old.db").write_text("mine too")
        for day in (1, 2, 3, 4):
            self.now = datetime(2026, 10, day, 8, 0, tzinfo=UTC)
            self.rotation(keep=2).run()

        self.assertEqual(
            self.names(),
            ["assistant-2026-10-03.db", "assistant-2026-10-04.db", "assistant-old.db", "notes.txt"],
        )

    def test_a_failed_run_keeps_the_previous_copy_and_leaves_no_partial_file(self):
        good = self.rotation().run()
        before = good.read_bytes()
        failing = MagicMock()

        def write_then_fail(destination):
            Path(destination).write_text("half written")
            raise BackupError("damaged")

        failing.backup.side_effect = write_then_fail

        with self.assertRaises(BackupError):
            self.rotation(store=failing).run()

        self.assertEqual(self.names(), ["assistant-2026-10-01.db"])
        self.assertEqual(good.read_bytes(), before)

    def test_keep_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.rotation(keep=0)


class StoreBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.destination = os.path.join(tmp.name, "copy.db")

    def test_file_store_in_wal_mode_is_copied_with_its_uncheckpointed_writes(self):
        source = SqliteDecisionStore(os.path.join(os.path.dirname(self.destination), "live.db"))
        self.addCleanup(source.close)
        source.set_state("key", "value")

        source.backup(self.destination)

        copy = SqliteDecisionStore(self.destination)
        self.addCleanup(copy.close)
        self.assertEqual(copy.get_state("key"), "value")

    def test_a_copy_failing_its_integrity_check_is_rejected(self):
        store = SqliteDecisionStore(":memory:")
        self.addCleanup(store.close)

        with (
            patch(
                "src.storage.decision_store._integrity_verdict",
                return_value="row 3 missing from index",
            ),
            self.assertRaisesRegex(BackupError, "row 3 missing"),
        ):
            store.backup(self.destination)


if __name__ == "__main__":
    unittest.main()
