import random
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src.domain import CallbackEvent, CommandEvent, DecisionRecord, EmailMessage, TriageResult
from src.interactions import InteractionHandler
from src.interactions.callbacks import Callback, parse_callback, review_buttons
from src.interactions.review import (
    NOTHING_TO_REVIEW,
    USAGE,
    format_review,
    parse_sample_size,
    select_sample,
)
from src.storage import SqliteDecisionStore


def record(message_id, route="label", sender=None, confidence=0.8) -> DecisionRecord:
    return DecisionRecord(
        message_id=message_id,
        thread_id="t",
        sender=sender or f"{message_id}@example.com",
        subject=f"subject {message_id}",
        excerpt="  some\n excerpt ",
        urgency="low",
        category="newsletter",
        confidence=confidence,
        route=route,
        created_at="2026-10-01T00:00:00+00:00",
        source="jev",
    )


def ids(records) -> list[str]:
    return [r.message_id for r in records]


class ParseSampleSizeTests(unittest.TestCase):
    def test_defaults_to_five_and_accepts_one_to_ten(self):
        self.assertEqual(parse_sample_size(""), 5)
        self.assertEqual(parse_sample_size("1"), 1)
        self.assertEqual(parse_sample_size("10"), 10)

    def test_out_of_range_or_non_numeric_is_refused(self):
        for args in ("0", "11", "-3", "abc", "2 3", "1.5"):
            with self.subTest(args=args):
                self.assertIsNone(parse_sample_size(args))


class SelectSampleTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(0)

    def test_half_archived_then_labeled_by_lowest_confidence(self):
        candidates = [
            record("r1", "reject"),
            record("r2", "reject"),
            record("r3", "reject"),
            record("l-sure", confidence=0.95),
            record("l-doubt", confidence=0.40),
            record("l-mid", confidence=0.70),
        ]

        sample = select_sample(candidates, 4, self.rng)

        self.assertEqual(len([r for r in sample if r.route == "reject"]), 2)
        self.assertEqual(ids(sample)[2:], ["l-doubt", "l-mid"])

    def test_odd_size_gives_the_extra_slot_to_archived_mail(self):
        candidates = [record(f"r{i}", "reject") for i in range(4)] + [
            record(f"l{i}") for i in range(4)
        ]

        sample = select_sample(candidates, 5, self.rng)

        self.assertEqual(len([r for r in sample if r.route == "reject"]), 3)

    def test_one_mail_per_sender_across_both_groups(self):
        candidates = [
            record("r1", "reject", sender="news@shop.com"),
            record("r2", "reject", sender="news@shop.com"),
            record("l1", sender="news@shop.com"),
            record("l2", sender="alice@example.com"),
        ]

        sample = select_sample(candidates, 4, self.rng)

        self.assertEqual(sorted(r.sender for r in sample), ["alice@example.com", "news@shop.com"])

    def test_a_short_group_is_topped_up_from_the_other(self):
        only_archived = [record(f"r{i}", "reject") for i in range(5)]
        only_labeled = [record(f"l{i}") for i in range(5)]

        self.assertEqual(len(select_sample(only_archived, 4, self.rng)), 4)
        self.assertEqual(len(select_sample(only_labeled, 4, self.rng)), 4)

    def test_no_candidate_gives_an_empty_sample(self):
        self.assertEqual(select_sample([], 5, self.rng), [])


class FormatReviewTests(unittest.TestCase):
    def test_shows_position_sender_subject_classification_and_a_flattened_excerpt(self):
        text = format_review(record("m1", "reject", confidence=0.82), 2, 5)

        self.assertEqual(
            text,
            "📋 À vérifier 2/5\n"
            "De : m1@example.com\n"
            "Objet : subject m1\n"
            "Classé : low · newsletter · archivé · confiance 82%\n\n"
            "some excerpt",
        )


class ReviewButtonsTests(unittest.TestCase):
    def test_buttons_depend_on_what_was_done_with_the_mail(self):
        labeled = [parse_callback(data) for _, data in review_buttons("m1", "label")[0]]
        archived = [parse_callback(data) for _, data in review_buttons("m1", "reject")[0]]

        self.assertEqual(
            labeled,
            [
                Callback("rv", "m1", "valid"),
                Callback("rv", "m1", "missed_important"),
                Callback("rv", "m1", "missed_urgent"),
                Callback("rv", "m1", "false_spam"),
            ],
        )
        self.assertEqual(
            archived,
            [
                Callback("rv", "m1", "valid"),
                Callback("rv", "m1", "wrong_archive"),
                Callback("rv", "m1", "missed_important"),
                Callback("rv", "m1", "missed_urgent"),
            ],
        )

    def test_alerted_mail_and_oversized_ids_get_no_review_buttons(self):
        self.assertIsNone(review_buttons("m1", "llm"))
        self.assertIsNone(review_buttons("x" * 60, "label"))


class ReviewCommandTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.chat = MagicMock()
        self.handler = InteractionHandler(self.store, self.chat, MagicMock())

    def decide(self, message_id, route="label", source="jev", days_ago=0):
        email = EmailMessage(
            id=message_id,
            thread_id="t",
            sender=f"{message_id}@example.com",
            subject=f"subject {message_id}",
            snippet="s",
            body="body",
        )
        now, self.now = self.now, self.now - timedelta(days=days_ago)
        self.store.record_decision(email, TriageResult("low", "newsletter", 0.8, source), route)
        self.now = now

    def review(self, args="", message_id=10):
        self.handler.dispatch(CommandEvent(message_id=message_id, name="review", args=args))

    def sent_texts(self) -> list[str]:
        return [call.args[0] for call in self.chat.send_message.call_args_list]

    def test_sends_one_message_per_mail_with_route_specific_buttons(self):
        self.decide("archived", route="reject")
        self.decide("labeled")

        self.review("2")

        calls = self.chat.send_message.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("À vérifier 1/2", calls[0].args[0])
        self.assertEqual(calls[0].kwargs["buttons"], review_buttons("archived", "reject"))
        self.assertEqual(calls[1].kwargs["buttons"], review_buttons("labeled", "label"))

    def test_skips_alerts_rule_decisions_rated_and_old_mail(self):
        self.decide("alerted", route="llm")
        self.decide("ruled", route="reject", source="rule")
        self.decide("old", days_ago=8)
        self.decide("rated")
        self.store.record_feedback("rated", "valid")
        self.decide("fresh")

        self.review()

        self.assertEqual(len(self.sent_texts()), 1)
        self.assertIn("subject fresh", self.sent_texts()[0])

    def test_says_so_when_there_is_nothing_to_review(self):
        self.review()

        self.assertEqual(self.sent_texts(), [NOTHING_TO_REVIEW])
        self.assertEqual(self.chat.send_message.call_args.kwargs["reply_to"], 10)

    def test_invalid_size_gets_the_usage(self):
        self.decide("labeled")

        self.review("beaucoup")

        self.assertEqual(self.sent_texts(), [USAGE])

    def test_a_review_verdict_is_stored_with_its_origin_and_counted_by_route(self):
        self.decide("archived", route="reject")
        self.review()
        data = self.chat.send_message.call_args.kwargs["buttons"][0][2][1]

        self.handler.dispatch(CallbackEvent(callback_id="cb", message_id=77, data=data))

        self.assertEqual(self.store.feedback_counts()["missed_important"], 1)
        row = self.store._conn.execute("SELECT verdict, origin FROM feedback").fetchone()
        self.assertEqual(tuple(row), ("missed_important", "review"))
        self.chat.clear_buttons.assert_called_once_with(77)

    def test_a_rated_mail_does_not_come_back(self):
        self.decide("labeled")
        self.review(message_id=10)
        self.handler.dispatch(CallbackEvent(callback_id="cb", message_id=77, data="rv:v:labeled"))
        self.chat.send_message.reset_mock()

        self.review(message_id=11)

        self.assertEqual(self.sent_texts(), [NOTHING_TO_REVIEW])


if __name__ == "__main__":
    unittest.main()
