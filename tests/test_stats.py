import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src.domain import CommandEvent, EmailMessage, FeedbackTally, TriageResult
from src.interactions import InteractionHandler
from src.interactions.stats import NO_FEEDBACK, format_stats
from src.storage import SqliteDecisionStore


class FormatStatsTests(unittest.TestCase):
    def test_precision_and_mistakes_per_decision_and_per_source(self):
        tallies = [
            FeedbackTally("llm", "jev", "valid", 3),
            FeedbackTally("llm", "jev", "false_urgent", 1),
            FeedbackTally("reject", "jev", "valid", 1),
            FeedbackTally("reject", "heuristic", "wrong_archive", 1),
            FeedbackTally("label", "", "missed_urgent", 2),
        ]

        text = format_stats(tallies, {"jev": 40, "rule": 12, "": 3})

        self.assertEqual(
            text,
            "📊 Précision du tri — verdicts : 8\n\n"
            "Par décision\n"
            "• alerté : 75% corrects sur 4 · faux urgent ×1\n"
            "• étiqueté : 0% corrects sur 2 · urgent raté ×2\n"
            "• archivé : 50% corrects sur 2 · à garder ×1\n\n"
            "Par source\n"
            "• inconnue : 0% corrects sur 2 · urgent raté ×2\n"
            "• heuristic : 0% corrects sur 1 · à garder ×1\n"
            "• jev : 80% corrects sur 5 · faux urgent ×1\n\n"
            "Décisions sur 30 jours\n"
            "jev 40 · rule 12 · inconnue 3",
        )

    def test_without_any_verdict_it_says_how_to_give_one(self):
        self.assertEqual(format_stats([], {}), NO_FEEDBACK)
        self.assertTrue(format_stats([], {"jev": 2}).startswith(NO_FEEDBACK))
        self.assertIn("jev 2", format_stats([], {"jev": 2}))


class StatsStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def decide(self, message_id, route, source, verdict=None):
        email = EmailMessage(
            id=message_id, thread_id="t", sender="a@b.com", subject="s", snippet="s", body="b"
        )
        self.store.record_decision(email, TriageResult("low", "personnel", 0.8, source), route)
        if verdict:
            self.store.record_feedback(message_id, verdict)

    def test_breakdown_groups_verdicts_by_route_and_source(self):
        self.decide("a", "llm", "jev", "valid")
        self.decide("b", "llm", "jev", "valid")
        self.decide("c", "llm", "jev", "false_urgent")
        self.decide("d", "reject", "heuristic", "wrong_archive")
        self.decide("unrated", "label", "jev")

        self.assertEqual(
            self.store.feedback_breakdown(),
            [
                FeedbackTally("llm", "jev", "false_urgent", 1),
                FeedbackTally("llm", "jev", "valid", 2),
                FeedbackTally("reject", "heuristic", "wrong_archive", 1),
            ],
        )

    def test_decision_volume_by_source_covers_the_window_only(self):
        self.decide("old", "label", "jev")
        self.now += timedelta(days=31)
        self.decide("a", "label", "jev")
        self.decide("b", "reject", "jev")
        self.decide("c", "reject", "rule")

        self.assertEqual(
            self.store.decision_counts_by_source(timedelta(days=30)), {"jev": 2, "rule": 1}
        )

    def test_stats_command_answers_in_the_chat(self):
        chat = MagicMock()
        handler = InteractionHandler(self.store, chat, MagicMock())
        self.decide("a", "llm", "jev", "valid")

        handler.dispatch(CommandEvent(message_id=10, name="stats"))

        self.assertIn("alerté : 100% corrects sur 1", chat.send_message.call_args.args[0])
        self.assertEqual(chat.send_message.call_args.kwargs["reply_to"], 10)

    def test_help_lists_review_and_stats(self):
        chat = MagicMock()
        handler = InteractionHandler(self.store, chat, MagicMock())

        handler.dispatch(CommandEvent(message_id=10, name="help"))

        text = chat.send_message.call_args.args[0]
        self.assertIn("/review", text)
        self.assertIn("/stats", text)


if __name__ == "__main__":
    unittest.main()
