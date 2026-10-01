import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta

from src.domain import VERDICTS, Correction, DecisionRecord, EmailMessage, TriageResult
from src.storage import SqliteDecisionStore
from src.storage.decision_store import EXCERPT_CHARS


def make_email(message_id="m1", body="body text", snippet="snippet", **overrides):
    email = EmailMessage(
        id=message_id,
        thread_id=f"t-{message_id}",
        sender="alice@example.com",
        subject=f"subject {message_id}",
        snippet=snippet,
        body=body,
    )
    for name, value in overrides.items():
        setattr(email, name, value)
    return email


NO_FEEDBACK = dict.fromkeys(VERDICTS, 0)


def make_triage(urgency="high", category="alerte_technique", confidence=0.9):
    return TriageResult(urgency=urgency, category=category, confidence=confidence)


class DecisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)

    def tearDown(self):
        self.store.close()

    def test_record_decision_then_get_returns_full_record(self):
        email = make_email(message_id_header="<abc@mail>")
        self.store.record_decision(email, make_triage(), "alert")

        self.assertEqual(
            self.store.get("m1"),
            DecisionRecord(
                message_id="m1",
                thread_id="t-m1",
                sender="alice@example.com",
                subject="subject m1",
                excerpt="body text",
                urgency="high",
                category="alerte_technique",
                confidence=0.9,
                route="alert",
                created_at="2026-01-01T12:00:00+00:00",
                chat_message_id=None,
                message_id_header="<abc@mail>",
            ),
        )

    def test_get_unknown_message_returns_none(self):
        self.assertIsNone(self.store.get("missing"))

    def test_excerpt_is_truncated_body(self):
        self.store.record_decision(make_email(body="x" * 1000), make_triage(), "alert")
        self.assertEqual(self.store.get("m1").excerpt, "x" * EXCERPT_CHARS)

    def test_excerpt_falls_back_to_snippet_when_body_empty(self):
        self.store.record_decision(make_email(body="", snippet="the snippet"), make_triage(), "a")
        self.assertEqual(self.store.get("m1").excerpt, "the snippet")

    def test_message_id_header_defaults_to_empty_when_email_lacks_it(self):
        self.store.record_decision(make_email(), make_triage(), "alert")
        self.assertEqual(self.store.get("m1").message_id_header, "")

    def test_upsert_updates_fields_and_preserves_chat_message_id(self):
        self.store.record_decision(make_email(), make_triage(), "alert")
        self.store.attach_chat_message("m1", 42)

        self.store.record_decision(make_email(), make_triage(urgency="low"), "archive")

        record = self.store.get("m1")
        self.assertEqual(record.urgency, "low")
        self.assertEqual(record.route, "archive")
        self.assertEqual(record.chat_message_id, 42)

    def test_attach_and_find_by_chat_message(self):
        self.store.record_decision(make_email(), make_triage(), "alert")
        self.store.attach_chat_message("m1", 7)

        self.assertEqual(self.store.find_by_chat_message(7).message_id, "m1")
        self.assertIsNone(self.store.find_by_chat_message(8))

    def test_attach_chat_message_to_unknown_message_is_noop(self):
        self.store.attach_chat_message("missing", 7)
        self.assertIsNone(self.store.find_by_chat_message(7))

    def test_record_feedback_unknown_message_returns_false(self):
        self.assertFalse(self.store.record_feedback("missing", "valid"))
        self.assertEqual(self.store.feedback_counts(), dict.fromkeys(VERDICTS, 0))

    def test_record_feedback_rejects_invalid_verdict(self):
        self.store.record_decision(make_email(), make_triage(), "alert")
        with self.assertRaises(ValueError):
            self.store.record_feedback("m1", "maybe")

    def test_latest_feedback_wins_with_one_row_per_message(self):
        self.store.record_decision(make_email(), make_triage(), "alert")
        self.assertTrue(self.store.record_feedback("m1", "false_urgent"))
        self.assertTrue(self.store.record_feedback("m1", "valid"))

        self.assertEqual(self.store.feedback_counts(), {**NO_FEEDBACK, "valid": 1})
        self.assertEqual(self.store.recent_corrections(10), [])

    def test_feedback_counts_has_every_verdict(self):
        for message_id, verdict in [("a", "valid"), ("b", "false_spam"), ("c", "false_spam")]:
            self.store.record_decision(make_email(message_id), make_triage(), "alert")
            self.store.record_feedback(message_id, verdict)

        self.assertEqual(
            self.store.feedback_counts(), {**NO_FEEDBACK, "valid": 1, "false_spam": 2}
        )

    def test_recent_corrections_excludes_valid_newest_first_and_limited(self):
        for message_id, verdict in [
            ("a", "false_urgent"),
            ("b", "valid"),
            ("c", "false_spam"),
            ("d", "false_urgent"),
        ]:
            self.store.record_decision(make_email(message_id), make_triage(), "alert")
            self.store.record_feedback(message_id, verdict)

        corrections = self.store.recent_corrections(2)

        self.assertEqual([c.subject for c in corrections], ["subject d", "subject c"])
        self.assertEqual(
            corrections[1],
            Correction(
                sender="alice@example.com",
                subject="subject c",
                excerpt="body text",
                predicted_urgency="high",
                predicted_category="alerte_technique",
                verdict="false_spam",
            ),
        )

    def test_re_recorded_feedback_moves_to_newest(self):
        for message_id in ("a", "b"):
            self.store.record_decision(make_email(message_id), make_triage(), "alert")
            self.store.record_feedback(message_id, "false_urgent")
        self.store.record_feedback("a", "false_spam")

        corrections = self.store.recent_corrections(10)

        self.assertEqual([(c.subject, c.verdict) for c in corrections], [
            ("subject a", "false_spam"),
            ("subject b", "false_urgent"),
        ])

    def test_recent_corrections_can_be_limited_to_some_verdicts(self):
        for message_id, verdict in [
            ("a", "missed_urgent"),
            ("b", "wrong_archive"),
            ("c", "wrong_archive"),
        ]:
            self.store.record_decision(make_email(message_id), make_triage(), "label")
            self.store.record_feedback(message_id, verdict, origin="review")

        usable = self.store.recent_corrections(1, ("false_urgent", "missed_urgent"))

        self.assertEqual([c.verdict for c in usable], ["missed_urgent"])
        self.assertEqual(len(self.store.recent_corrections(10)), 3)

    def test_feedback_origin_is_stored_and_validated(self):
        self.store.record_decision(make_email(), make_triage(), "label")

        self.store.record_feedback("m1", "valid")
        self.assertEqual(self._origin(), "alert")
        self.store.record_feedback("m1", "missed_urgent", origin="review")
        self.assertEqual(self._origin(), "review")
        with self.assertRaises(ValueError):
            self.store.record_feedback("m1", "valid", origin="elsewhere")

    def _origin(self) -> str:
        return self.store._conn.execute("SELECT origin FROM feedback").fetchone()[0]

    def test_review_candidates_are_recent_unrated_silent_classifier_decisions(self):
        def decide(message_id, route, source="jev"):
            triage = TriageResult("low", "newsletter", 0.8, source)
            self.store.record_decision(make_email(message_id), triage, route)

        self.now = datetime(2025, 12, 20, tzinfo=UTC)
        decide("old", "label")
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        decide("alerted", "llm")
        decide("ruled", "reject", source="rule")
        decide("rated", "label")
        self.store.record_feedback("rated", "valid")
        decide("archived", "reject")
        decide("labeled", "label", source="heuristic")

        candidates = self.store.review_candidates(timedelta(days=7), 10)

        self.assertEqual([c.message_id for c in candidates], ["labeled", "archived"])
        self.assertEqual(len(self.store.review_candidates(timedelta(days=7), 1)), 1)
        self.assertEqual(self.store.review_candidates(timedelta(days=7), 0), [])

    def test_recent_corrections_non_positive_limit_returns_empty(self):
        self.store.record_decision(make_email(), make_triage(), "alert")
        self.store.record_feedback("m1", "false_spam")
        self.assertEqual(self.store.recent_corrections(0), [])
        self.assertEqual(self.store.recent_corrections(-1), [])

    def test_was_alerted(self):
        self.assertFalse(self.store.was_alerted("m1"))
        self.store.mark_alerted("m1")
        self.store.mark_alerted("m1")
        self.assertTrue(self.store.was_alerted("m1"))
        self.assertFalse(self.store.was_alerted("m2"))

    def test_was_alerted_within_window_ignores_older_alerts(self):
        self.store.mark_alerted("m1")
        self.now += timedelta(hours=25)

        self.assertTrue(self.store.was_alerted("m1"))
        self.assertFalse(self.store.was_alerted("m1", within_seconds=24 * 3600))
        self.assertTrue(self.store.was_alerted("m1", within_seconds=26 * 3600))

    def test_re_alert_refreshes_the_window(self):
        self.store.mark_alerted("m1")
        self.now += timedelta(hours=25)
        self.store.mark_alerted("m1")

        self.assertTrue(self.store.was_alerted("m1", within_seconds=3600))

    def test_find_by_chat_message_prefers_the_newest_decision(self):
        self.store.record_decision(make_email("old"), make_triage(), "alert")
        self.store.attach_chat_message("old", 7)
        self.now += timedelta(minutes=1)
        self.store.record_decision(make_email("new"), make_triage(), "alert")
        self.store.attach_chat_message("new", 7)

        self.assertEqual(self.store.find_by_chat_message(7).message_id, "new")

    def test_state_get_and_overwrite(self):
        self.assertIsNone(self.store.get_state("history_id"))
        self.store.set_state("history_id", "1")
        self.store.set_state("history_id", "2")
        self.assertEqual(self.store.get_state("history_id"), "2")

    def test_prune_removes_only_expired_unrated_decisions_alerts_and_dedup_state(self):
        for message_id in ("old", "rated"):
            self.store.record_decision(make_email(message_id), make_triage(), "alert")
            self.store.mark_alerted(message_id)
        self.store.record_feedback("rated", "false_urgent")
        for key in ("reply:1", "draft_sent:d1", "bot_draft:d1", "telegram_offset", "replyx"):
            self.store.set_state(key, "v")
        self.now += timedelta(days=91)
        self.store.record_decision(make_email("fresh"), make_triage(), "alert")
        self.store.mark_alerted("fresh")
        self.store.set_state("reply:2", "v")

        deleted = self.store.prune(timedelta(days=90))

        self.assertEqual(deleted, 1 + 2 + 3)
        self.assertIsNone(self.store.get("old"))
        self.assertIsNotNone(self.store.get("rated"))
        self.assertIsNotNone(self.store.get("fresh"))
        self.assertEqual(self.store.recent_corrections(5)[0].subject, "subject rated")
        self.assertFalse(self.store.was_alerted("old"))
        self.assertTrue(self.store.was_alerted("fresh"))
        for key in ("reply:1", "draft_sent:d1", "bot_draft:d1"):
            self.assertIsNone(self.store.get_state(key))
        for key in ("telegram_offset", "replyx", "reply:2"):
            self.assertEqual(self.store.get_state(key), "v")

    def test_prune_prefix_is_literal_not_a_wildcard(self):
        self.store.set_state("draftXsent:1", "v")
        self.now += timedelta(days=91)

        self.assertEqual(self.store.prune(timedelta(days=90)), 0)
        self.assertEqual(self.store.get_state("draftXsent:1"), "v")

    def test_overwritten_state_is_not_pruned(self):
        self.store.set_state("reply:1", "a")
        self.now += timedelta(days=91)
        self.store.set_state("reply:1", "b")

        self.store.prune(timedelta(days=90))

        self.assertEqual(self.store.get_state("reply:1"), "b")

    def test_created_at_uses_clock(self):
        self.now += timedelta(hours=1)
        self.store.record_decision(make_email(), make_triage(), "alert")
        self.assertEqual(self.store.get("m1").created_at, "2026-01-01T13:00:00+00:00")

    def test_concurrent_writes_from_threads(self):
        def write(prefix):
            for i in range(50):
                self.store.record_decision(make_email(f"{prefix}{i}"), make_triage(), "alert")
                self.store.mark_alerted(f"{prefix}{i}")

        threads = [threading.Thread(target=write, args=(p,)) for p in "abcd"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertTrue(all(self.store.was_alerted(f"{p}{i}") for p in "abcd" for i in range(50)))


class DecisionStoreFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "nested", "dir", "store.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_parent_directory_and_persists_across_reopen(self):
        store = SqliteDecisionStore(self.path)
        store.record_decision(make_email(), make_triage(), "alert")
        store.attach_chat_message("m1", 99)
        store.record_feedback("m1", "false_urgent")
        store.mark_alerted("m1")
        store.set_state("history_id", "123")
        store.close()

        reopened = SqliteDecisionStore(self.path)
        try:
            self.assertEqual(reopened.find_by_chat_message(99).message_id, "m1")
            self.assertEqual(reopened.recent_corrections(5)[0].verdict, "false_urgent")
            self.assertTrue(reopened.was_alerted("m1"))
            self.assertEqual(reopened.get_state("history_id"), "123")
        finally:
            reopened.close()

    def test_file_database_uses_wal(self):
        SqliteDecisionStore(self.path).close()
        conn = sqlite3.connect(self.path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode, "wal")

    def test_creates_private_database_and_directory(self):
        store = SqliteDecisionStore(self.path)
        store.record_decision(make_email(), make_triage(), "alert")
        try:
            self.assertEqual(_mode(self.path), 0o600)
            self.assertEqual(_mode(os.path.dirname(self.path)), 0o700)
            self.assertEqual(_mode(f"{self.path}-wal"), 0o600)
        finally:
            store.close()

    def test_existing_directory_and_database_modes_are_left_alone(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o755)
        os.chmod(directory, 0o755)
        SqliteDecisionStore(self.path).close()
        os.chmod(self.path, 0o640)

        SqliteDecisionStore(self.path).close()

        self.assertEqual(_mode(directory), 0o755)
        self.assertEqual(_mode(self.path), 0o640)


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
