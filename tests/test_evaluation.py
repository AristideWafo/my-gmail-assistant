import contextlib
import io
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from src.config import Settings
from src.domain import VERDICTS, DecisionRecord, EmailMessage, RatedDecision, TriageResult
from src.evaluation.__main__ import _variants, main
from src.evaluation.dataset import EXPECTATIONS, Case, build_dataset, default_split
from src.evaluation.runner import MIN_CORRECTIONS, evaluate, format_report, is_conclusive
from src.gmail.client import GmailClient
from src.storage import SqliteDecisionStore
from src.workflow import route_for
from tests.fakes import FakeClassifier, FakeMail


def email(message_id="m1", sender="alice@example.com", subject="Hello") -> EmailMessage:
    return EmailMessage(
        id=message_id, thread_id="t", sender=sender, subject=subject, snippet="s", body="b"
    )


def rated(message_id, verdict, route="llm", rated_at="2026-01-01") -> RatedDecision:
    record = DecisionRecord(
        message_id=message_id,
        thread_id="t",
        sender="alice@example.com",
        subject=f"subject {message_id}",
        excerpt="excerpt",
        urgency="high",
        category="personnel",
        confidence=0.9,
        route=route,
        created_at="2025-12-31",
    )
    return RatedDecision(record=record, verdict=verdict, rated_at=rated_at)


def case(verdict, recorded_route="llm", **email_fields) -> Case:
    return Case(email(**email_fields), verdict, recorded_route, "2026-01-01")


class ExpectationTests(unittest.TestCase):
    def test_each_verdict_constrains_the_replayed_route(self):
        expected = {
            ("valid", "llm"): {"llm"},
            ("valid", "label"): {"label"},
            ("false_urgent", "llm"): {"label", "reject"},
            ("false_spam", "llm"): {"reject"},
            ("missed_urgent", "label"): {"llm"},
            ("wrong_archive", "reject"): {"llm", "label"},
            ("missed_important", "label"): {"llm", "label"},
        }
        for (verdict, recorded), accepted in expected.items():
            with self.subTest(verdict=verdict):
                passing = {
                    route
                    for route in ("llm", "label", "reject")
                    if case(verdict, recorded).satisfied_by(route)
                }
                self.assertEqual(passing, accepted)

    def test_every_verdict_the_app_can_store_has_an_expectation(self):
        self.assertLessEqual(set(VERDICTS), set(EXPECTATIONS))


class BuildDatasetTests(unittest.TestCase):
    def setUp(self):
        self.mail = FakeMail(unread=[email(f"m{i}") for i in range(1, 7)])

    def test_default_split_leaves_half_of_the_corrections_for_training(self):
        items = [
            rated("m1", "false_urgent", rated_at="2026-01-01"),
            rated("m2", "valid", rated_at="2026-01-02"),
            rated("m3", "false_spam", rated_at="2026-01-03"),
            rated("m4", "false_urgent", rated_at="2026-01-04"),
            rated("m5", "valid", rated_at="2026-01-05"),
            rated("m6", "false_urgent", rated_at="2026-01-06"),
        ]

        dataset = build_dataset(items, self.mail)

        self.assertEqual(dataset.split_at, "2026-01-04")
        self.assertEqual([c.subject for c in dataset.training], ["subject m1", "subject m3"])
        self.assertEqual([c.email.id for c in dataset.cases], ["m4", "m5", "m6"])

    def test_training_mails_are_never_scored(self):
        items = [
            rated("m1", "false_urgent", rated_at="2026-01-01"),
            rated("m2", "false_urgent", rated_at="2026-01-02"),
        ]

        dataset = build_dataset(items, self.mail)

        scored = {c.email.id for c in dataset.cases}
        trained = {c.subject for c in dataset.training}
        self.assertEqual((scored, trained), ({"m2"}, {"subject m1"}))

    def test_too_few_corrections_means_no_split_and_everything_is_scored(self):
        items = [rated("m1", "valid"), rated("m2", "false_urgent")]

        dataset = build_dataset(items, self.mail)

        self.assertEqual((dataset.split_at, dataset.training), ("", []))
        self.assertEqual(len(dataset.cases), 2)
        self.assertEqual(default_split(items), "")

    def test_explicit_split_overrides_the_default(self):
        items = [rated(f"m{i}", "false_urgent", rated_at=f"2026-01-0{i}") for i in range(1, 5)]

        dataset = build_dataset(items, self.mail, split_at="2026-01-02")

        self.assertEqual([c.email.id for c in dataset.cases], ["m2", "m3", "m4"])

    def test_deleted_mails_and_unknown_verdicts_are_left_out(self):
        items = [rated("gone", "valid"), rated("m1", "valid"), rated("m2", "bogus")]

        dataset = build_dataset(items, self.mail)

        self.assertEqual(([c.email.id for c in dataset.cases], dataset.missing), (["m1"], 1))


class EvaluateTests(unittest.TestCase):
    def test_scores_each_case_against_its_verdict(self):
        cases = [case("valid", "llm"), case("false_urgent"), case("missed_urgent", "label")]
        always_urgent = FakeClassifier(TriageResult("high", "personnel", 0.9))

        report = evaluate("urgent", always_urgent, cases, 0.5)

        self.assertEqual((report.overall.passed, report.overall.total), (2, 3))
        self.assertEqual(report.by_verdict["false_urgent"].passed, 0)
        self.assertEqual(report.classifier_calls, 3)

    def test_rules_are_applied_first_and_cost_no_classifier_call(self):
        ruled = case("valid", "reject", sender="info@news.leboncoin.fr")
        classifier = MagicMock()

        report = evaluate("any", classifier, [ruled], 0.5)

        classifier.classify.assert_not_called()
        self.assertEqual((report.overall.passed, report.classifier_calls), (1, 0))

    def test_a_classifier_error_is_a_miss_and_the_run_continues(self):
        classifier = MagicMock()
        classifier.classify.side_effect = [RuntimeError("api down"), TriageResult("high", "personnel", 0.9)]

        with self.assertLogs("src.evaluation.runner", level="WARNING"):
            report = evaluate("flaky", classifier, [case("valid"), case("valid")], 0.5)

        self.assertEqual((report.overall.passed, report.errors), (1, 1))

    def test_small_samples_are_flagged_as_not_conclusive(self):
        few = [case("false_urgent")] * MIN_CORRECTIONS
        enough = few + [case("valid")] * 80
        report = evaluate("h", FakeClassifier(), few, 0.5)

        self.assertFalse(is_conclusive(few))
        self.assertTrue(is_conclusive(enough))
        self.assertIn("NOT CONCLUSIVE", format_report([report], few, 0, ""))
        self.assertNotIn("NOT CONCLUSIVE", format_report([report], enough, 0, ""))

    def test_report_is_an_aligned_table_with_one_row_per_variant(self):
        cases = [case("valid", "label"), case("false_urgent")]
        reports = [
            evaluate("heuristic", FakeClassifier(), cases, 0.5),
            evaluate("urgent", FakeClassifier(TriageResult("high", "personnel", 0.9)), cases, 0.5),
        ]

        text = format_report(reports, cases, missing=1, split_at="2026-01-04")

        self.assertIn("Cases: 2 (1 corrections), 1 mail(s) no longer available", text)
        self.assertIn("Split: 2026-01-04", text)
        self.assertIn("heuristic  100% (2/2)  100% (1/1)    100% (1/1)  2      0", text)
        self.assertIn("urgent     0% (0/2)    0% (0/1)      0% (0/1)    2      0", text)


class RouteForTests(unittest.TestCase):
    def test_matches_the_workflow_routing(self):
        self.assertEqual(route_for(TriageResult("high", "personnel", 0.9), 0.5), "llm")
        self.assertEqual(route_for(TriageResult("low", "newsletter", 0.9), 0.5), "reject")
        self.assertEqual(route_for(TriageResult("low", "newsletter", 0.4), 0.5), "label")
        self.assertEqual(route_for(TriageResult("medium", "personnel", 0.9), 0.5), "label")


class FetchMessageTests(unittest.TestCase):
    def make_client(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")
        client._service = MagicMock()
        return client, client._service.users().messages().get().execute

    def test_returns_the_parsed_message(self):
        client, execute = self.make_client()
        execute.return_value = {
            "id": "m1",
            "threadId": "t1",
            "payload": {"headers": [{"name": "From", "value": "Jane <jane@example.com>"}]},
        }

        message = client.fetch_message("m1")

        self.assertEqual((message.id, message.sender), ("m1", "jane@example.com"))

    def test_a_deleted_message_is_none_and_other_errors_propagate(self):
        client, execute = self.make_client()
        execute.side_effect = HttpError(resp=MagicMock(status=404), content=b"gone")
        self.assertIsNone(client.fetch_message("m1"))

        execute.side_effect = HttpError(resp=MagicMock(status=500), content=b"boom")
        with self.assertRaises(HttpError):
            client.fetch_message("m1")

    def test_unconfigured_client_returns_none(self):
        self.assertIsNone(GmailClient("", "", "").fetch_message("m1"))


class StoreQueriesTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)

    def decide(self, message_id, sender, urgency="low", category="newsletter", source="jev"):
        self.store.record_decision(
            email(message_id, sender=sender), TriageResult(urgency, category, 0.9, source), "reject"
        )

    def test_rated_decisions_are_returned_oldest_verdict_first(self):
        self.decide("a", "x@a.com")
        self.decide("b", "x@b.com")
        self.decide("unrated", "x@c.com")
        self.store.record_feedback("b", "valid")
        self.store.record_feedback("a", "false_spam")

        found = self.store.rated_decisions()

        self.assertEqual([(r.record.message_id, r.verdict) for r in found], [("b", "valid"), ("a", "false_spam")])
        self.assertEqual(found[0].record.source, "jev")

    def test_rule_candidates_are_frequent_consistent_uncorrected_jev_senders(self):
        for i in range(3):
            self.decide(f"steady{i}", "news@steady.com")
            self.decide(f"ruled{i}", "news@ruled.com", source="rule")
            self.decide(f"fixed{i}", "news@corrected.com")
        self.decide("mixed1", "bot@mixed.com")
        self.decide("mixed2", "bot@mixed.com", urgency="high", category="alerte_technique")
        self.decide("mixed3", "bot@mixed.com")
        self.decide("rare", "once@rare.com")
        self.store.record_feedback("fixed0", "false_spam")

        found = self.store.rule_candidates(3, timedelta(days=90))

        self.assertEqual(
            [(c.sender, c.urgency, c.category, c.count) for c in found],
            [("news@steady.com", "low", "newsletter", 3)],
        )


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "assistant.db")
        env = patch.dict(os.environ, {"DB_PATH": self.db_path, "JEV_API_KEY": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("src.evaluation.__main__.Settings") as settings:
            settings.return_value = Settings(_env_file=None, db_path=self.db_path)
            code = main(list(argv))
        return code, out.getvalue()

    def seed(self):
        store = SqliteDecisionStore(self.db_path)
        store.record_decision(email("m1"), TriageResult("low", "personnel", 0.6, "heuristic"), "label")
        store.record_feedback("m1", "valid")
        store.close()

    def test_run_replays_rated_mails_with_the_heuristic_when_jev_is_not_configured(self):
        self.seed()
        mail = FakeMail(unread=[email("m1")])

        with patch.dict("src.evaluation.__main__.MAIL_PROVIDERS", {"gmail": lambda ctx: mail}):
            code, output = self.run_cli("run")

        self.assertEqual(code, 0)
        self.assertIn("heuristic  100% (1/1)", output)
        self.assertNotIn("jev", output)
        self.assertIn("NOT CONCLUSIVE", output)

    def test_check_examples_needs_a_configured_jev(self):
        code, output = self.run_cli("check-examples")

        self.assertEqual(code, 2)
        self.assertIn("not configured", output)

    def test_check_examples_reports_what_the_api_did(self):
        configured = Settings(_env_file=None, db_path=self.db_path, jev_api_key="k")
        accepted = TriageResult("low", "newsletter", 0.9, "jev")

        for outcome, code, word in ((accepted, 0, "ACCEPTED"), (RuntimeError("400"), 1, "REJECTED")):
            out = io.StringIO()
            with (
                self.subTest(word=word),
                contextlib.redirect_stdout(out),
                patch("src.evaluation.__main__.Settings", return_value=configured),
                patch("src.evaluation.__main__.JevClassifier.classify", side_effect=[outcome]),
            ):
                self.assertEqual(main(["check-examples"]), code)
                self.assertIn(word, out.getvalue())

    def test_few_shot_variant_needs_a_configured_jev_and_examples(self):
        configured = Settings(_env_file=None, jev_api_key="k")
        examples = [{"subject": "s"}]

        self.assertEqual(list(_variants(Settings(_env_file=None), examples, set())), ["heuristic"])
        self.assertEqual(list(_variants(configured, [], set())), ["heuristic", "jev"])
        self.assertEqual(
            list(_variants(configured, examples, set())), ["heuristic", "jev", "jev+few-shot"]
        )
        self.assertEqual(list(_variants(configured, examples, {"jev"})), ["jev"])

    def test_attention_says_how_to_start_when_nothing_was_asked(self):
        self.seed()

        code, output = self.run_cli("attention")

        self.assertEqual(code, 0)
        self.assertIn("ATTENTION_MODE=shadow", output)

    def test_attention_reads_the_stored_answers_without_any_call(self):
        store = SqliteDecisionStore(self.db_path)
        for message_id, category, route, signals in (
            ("cut", "notification_systeme", "reject", {"service_change": 0.99}),
            ("promo", "promotion", "reject", {"personal_deadline": 0.9}),
            ("alerted", "personnel", "llm", {"personal_event": 0.9}),
            ("plain", "personnel", "label", {"personal_event": 0.1}),
        ):
            triage = TriageResult("low", category, 0.9, "jev", 0.1, signals)
            store.record_decision(email(message_id, subject=f"about {message_id}"), triage, route)
        store.close()

        code, output = self.run_cli("attention", "--days", "7")

        self.assertEqual(code, 0)
        self.assertIn("Mails asked the attention questions: 4 over 1 day(s)", output)
        self.assertIn("Would be put forward at 0.5: 1 (1.0 per day), of which 1 are archived", output)
        self.assertIn("Also yes on 1 mail(s) already alerted", output)
        self.assertIn("By reason: service_change 1", output)
        self.assertIn("example.com «about cut» [service_change; reject]", output)
        self.assertNotIn("about promo", output)

    def test_candidates_reports_when_there_are_none(self):
        self.seed()

        code, output = self.run_cli("candidates")

        self.assertEqual(code, 0)
        self.assertIn("No sender", output)


if __name__ == "__main__":
    unittest.main()
