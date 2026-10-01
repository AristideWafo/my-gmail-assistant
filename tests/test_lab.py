import contextlib
import io
import os
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from src.config import Settings
from src.domain import EmailMessage, TriageResult
from src.errors import ConfigurationError
from src.evaluation.__main__ import main
from src.evaluation.corpus import CORPUS_TODAY, LabCase, cases_from_rated, load_corpus
from src.evaluation.dataset import Case
from src.evaluation.lab import format_lab_report, planned_calls, run_variant
from src.evaluation.variants import SIGNAL_QUESTIONS, VARIANTS
from src.storage import SqliteDecisionStore
from src.triage.engine import JevClassifier
from src.triage.rules import RuleSet, SenderRule
from tests.fakes import FakeMail


def email(message_id="m1", sender="alice@example.com", subject="Hello") -> EmailMessage:
    return EmailMessage(
        id=message_id, thread_id="t", sender=sender, subject=subject, snippet="s", body="b"
    )


def choice(value, confidence=0.9):
    return {"type": "choice", "choice": value, "confidence": confidence}


def answers(urgency="low", category="personnel", **extra):
    return {"urgency": choice(urgency), "category": choice(category), **extra}


class ScriptedJev(JevClassifier):
    """Answers from a script instead of the network; an exception in the script is raised."""

    def __init__(self, script, usage=None):
        super().__init__("https://jev.example", api_key="k")
        self.script = list(script)
        self.usage = usage or {"input_tokens": 800, "output_tokens": 100}
        self.requests = []

    def ask(self, request):
        self.requests.append(request)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"answers": outcome, "usage": self.usage}


def case(name="m1", routes=("label",), expected=None, today=""):
    return LabCase(name, email(name), set(routes).__contains__, expected, today)


class CorpusTests(unittest.TestCase):
    def test_shipped_corpus_is_valid_and_dated(self):
        cases = load_corpus()

        self.assertEqual(len(cases), 50)
        self.assertEqual(len({item.name for item in cases}), 50)
        self.assertTrue(all(item.today == CORPUS_TODAY for item in cases))
        self.assertTrue(all(item.email.received_at.startswith(CORPUS_TODAY) for item in cases))
        self.assertTrue(any(item.accepts("llm") for item in cases))
        self.assertTrue(any(item.accepts("reject") for item in cases))

    def test_expected_signals_merge_needs_reply_and_facts(self):
        by_name = {item.name: item for item in load_corpus()}

        self.assertEqual(
            by_name["perso_invitation"].expected_signals, {"needs_reply", "asks_for_meeting"}
        )
        self.assertEqual(by_name["perso_merci"].expected_signals, frozenset())

    def write(self, body: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "corpus.toml"
        path.write_text(body)
        return path

    def test_invalid_entries_are_refused_with_the_mail_name(self):
        base = 'name = "x"\nsender = "a@b.com"\nsubject = "s"\nbody = "b"\nneeds_reply = false\n'
        broken = {
            "unknown route": base + 'routes = ["inbox"]\nfacts = []\n',
            "no route": base + "routes = []\nfacts = []\n",
            "unknown fact": base + 'routes = ["label"]\nfacts = ["is_fun"]\n',
            "missing key": base + 'routes = ["label"]\n',
        }
        for reason, mail in broken.items():
            with self.subTest(reason), self.assertRaises(ConfigurationError) as raised:
                load_corpus(self.write(f"[[mail]]\n{mail}"))
            self.assertIn("'x'", str(raised.exception))

    def test_duplicated_names_are_refused(self):
        mail = (
            '[[mail]]\nname = "x"\nsender = "a@b.com"\nsubject = "s"\nbody = "b"\n'
            'needs_reply = false\nroutes = ["label"]\nfacts = []\n'
        )

        with self.assertRaises(ConfigurationError):
            load_corpus(self.write(mail + mail))


class RatedCasesTests(unittest.TestCase):
    def test_rated_mails_keep_their_verdict_constraint_and_have_no_signal_truth(self):
        cases, by_rule = cases_from_rated(
            [Case(email("m1"), "false_urgent", "llm", "2026-01-01")], RuleSet()
        )

        self.assertEqual(by_rule, 0)
        self.assertFalse(cases[0].accepts("llm"))
        self.assertTrue(cases[0].accepts("label"))
        self.assertIsNone(cases[0].expected_signals)
        self.assertEqual(cases[0].today, "")
        self.assertEqual(cases[0].name, "m1 example.com «Hello» [false_urgent, was llm]")

    def test_rated_mail_name_keeps_one_line_and_a_bounded_subject(self):
        subject = "Tr\xa0: COUPURE\nEAU " + "x" * 80
        cases, _ = cases_from_rated(
            [Case(email("m1", subject=subject), "valid", "label", "2026-01-01")], RuleSet()
        )

        self.assertIn("«Tr : COUPURE EAU xxx", cases[0].name)
        self.assertNotIn("\n", cases[0].name)
        self.assertLessEqual(len(cases[0].name.split("«")[1].split("»")[0]), 60)

    def test_mails_decided_by_a_rule_are_left_out_and_counted(self):
        rules = RuleSet(rules=(SenderRule(re.compile("news@shop"), "low", "newsletter"),))
        rated = [
            Case(email("m1", sender="news@shop.com"), "valid", "reject", "2026-01-01"),
            Case(email("m2"), "valid", "label", "2026-01-01"),
        ]

        cases, by_rule = cases_from_rated(rated, rules)

        self.assertEqual((len(cases), by_rule), (1, 1))


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.request = JevClassifier("https://jev.example", api_key="k").build_request(email())

    def test_current_sends_the_production_request_and_routes_like_production(self):
        current = VARIANTS["current"]

        self.assertEqual(current.prepare(self.request), self.request)
        self.assertEqual(current.route(answers("high", "personnel"), 0.5), "llm")
        self.assertEqual(current.route(answers("low", "newsletter"), 0.5), "reject")
        self.assertEqual(current.route(answers("low", "personnel"), 0.5), "label")

    def test_direct_action_replaces_urgency_and_maps_its_choice_to_a_route(self):
        variant = VARIANTS["direct-action"]
        prepared = variant.prepare(self.request)

        self.assertEqual(set(prepared["questions"]), {"action", "category"})
        self.assertEqual(prepared["state"], self.request["state"])
        for action, route in (("alert", "llm"), ("keep", "label"), ("archive", "reject")):
            self.assertEqual(variant.route({"action": choice(action)}, 0.5), route)

    def test_signals_adds_every_candidate_question_without_touching_the_others(self):
        variant = VARIANTS["signals"]
        prepared = variant.prepare(self.request)

        self.assertEqual(set(prepared["questions"]), {"urgency", "category", *SIGNAL_QUESTIONS})
        self.assertEqual(prepared["questions"]["urgency"], self.request["questions"]["urgency"])
        self.assertEqual(variant.signals, tuple(SIGNAL_QUESTIONS))
        self.assertEqual(set(self.request["questions"]), {"urgency", "category"})


class RunVariantTests(unittest.TestCase):
    def test_counts_correct_routes_per_run_and_lists_misrouted_and_unstable_mails(self):
        cases = [case("stable"), case("flaky"), case("wrong", routes=("reject",))]
        jev = ScriptedJev(
            [
                answers("low", "personnel"),
                answers("low", "personnel"),
                answers("low", "personnel"),
                answers("low", "personnel"),
                answers("high", "personnel"),
                answers("low", "personnel"),
            ]
        )

        report = run_variant(VARIANTS["current"], cases, jev, 0.5, repeats=2)

        self.assertEqual([(run.passed, run.total) for run in report.runs], [(2, 3), (1, 3)])
        self.assertEqual(report.unstable, ["flaky"])
        self.assertEqual(report.misrouted, ["flaky -> label/llm", "wrong -> label/label"])
        self.assertEqual(report.tokens, [900] * 6)
        self.assertEqual(report.errors, 0)

    def test_a_failed_call_is_a_miss_and_the_run_continues(self):
        jev = ScriptedJev([RuntimeError("boom"), answers("low", "personnel")])

        with self.assertLogs("src.evaluation.lab", level="WARNING"):
            report = run_variant(VARIANTS["current"], [case("a"), case("b")], jev, 0.5)

        self.assertEqual((report.runs[0].passed, report.errors), (1, 1))
        self.assertEqual(report.misrouted, ["a -> error"])
        self.assertEqual(len(report.latencies), 1)

    def test_an_unusable_answer_is_a_miss_too(self):
        jev = ScriptedJev([{"urgency": choice("low")}])

        with self.assertLogs("src.evaluation.lab", level="WARNING"):
            report = run_variant(VARIANTS["current"], [case()], jev, 0.5)

        self.assertEqual((report.runs[0].passed, report.errors), (0, 1))

    def test_corpus_date_replaces_today_and_real_mails_keep_the_real_date(self):
        jev = ScriptedJev([answers(), answers()])

        run_variant(VARIANTS["current"], [case("a", today="2020-02-03"), case("b")], jev, 0.5)

        self.assertEqual(jev.requests[0]["state"]["today"], "2020-02-03")
        self.assertEqual(jev.requests[1]["state"]["today"], datetime.now(UTC).date().isoformat())

    def test_latency_is_measured_around_each_call(self):
        ticks = iter([10.0, 10.25])

        report = run_variant(
            VARIANTS["current"], [case()], ScriptedJev([answers()]), 0.5, clock=lambda: next(ticks)
        )

        self.assertEqual(report.latencies, [0.25])

    def test_signals_are_compared_with_the_truth_when_it_is_known(self):
        def noul(value):
            return {"type": "noul", "noul": value}

        cases = [
            case("asks", expected=frozenset({"needs_reply"})),
            case("silent", expected=frozenset({"needs_reply"})),
            case("bait", expected=frozenset()),
        ]
        jev = ScriptedJev(
            [
                answers(needs_reply=noul(0.9)),
                answers(needs_reply=noul(0.2)),
                answers(needs_reply=noul(0.7), has_deadline=noul(0.5)),
            ]
        )

        report = run_variant(VARIANTS["signals"], cases, jev, 0.5)

        needs_reply = report.signals["needs_reply"]
        self.assertEqual(needs_reply.missed, ["silent"])
        self.assertEqual(needs_reply.unexpected, ["bait"])
        self.assertEqual(needs_reply.with_truth, 3)
        self.assertEqual(report.signals["has_deadline"].unexpected, ["bait"])
        self.assertEqual(report.signals["asks_for_meeting"].yes, [])

    def test_signals_without_truth_are_only_listed(self):
        jev = ScriptedJev([answers(needs_reply={"type": "noul", "noul": 0.9})])

        report = run_variant(VARIANTS["signals"], [case("real")], jev, 0.5)

        needs_reply = report.signals["needs_reply"]
        self.assertEqual((needs_reply.yes, needs_reply.with_truth), (["real"], 0))
        self.assertEqual((needs_reply.missed, needs_reply.unexpected), ([], []))

    def test_signals_are_read_on_the_first_run_only(self):
        yes = answers(needs_reply={"type": "noul", "noul": 0.9})

        report = run_variant(VARIANTS["signals"], [case("a")], ScriptedJev([yes, yes]), 0.5, 2)

        self.assertEqual(report.signals["needs_reply"].yes, ["a"])


class ReportTests(unittest.TestCase):
    def test_planned_calls_multiply_variants_mails_and_runs(self):
        self.assertEqual(planned_calls(list(VARIANTS.values()), [case("a"), case("b")], 3), 18)

    def test_report_has_one_row_per_variant_and_the_details_below(self):
        cases = [case("ok", expected=frozenset()), case("bad", routes=("reject",))]
        script = [answers(), answers()]
        reports = [
            run_variant(VARIANTS["current"], cases, ScriptedJev(script), 0.5),
            run_variant(VARIANTS["signals"], cases, ScriptedJev(script), 0.5),
        ]

        lines = format_lab_report(reports, len(cases)).splitlines()

        self.assertEqual(lines[0], "Cases: 2")
        self.assertTrue(lines[2].startswith("variant"))
        self.assertEqual(lines[3].split()[:5], ["current", "1/2", "0", "900", "0.00"])
        self.assertIn("  misrouted (1): bad -> label", lines)
        self.assertTrue(any(line.startswith("[signals] current questions plus") for line in lines))
        self.assertIn("  needs_reply: 0 missed, 0 unexpected on 1 mails", lines)

    def test_a_variant_whose_every_call_failed_still_prints(self):
        with self.assertLogs("src.evaluation.lab", level="WARNING"):
            report = run_variant(
                VARIANTS["current"], [case()], ScriptedJev([RuntimeError("boom")]), 0.5
            )

        row = format_lab_report([report], 1).splitlines()[3]

        self.assertEqual(row.split(), ["current", "0/1", "0", "-", "-", "-", "1"])


class LabCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "assistant.db")

    def run_cli(self, *argv, jev_api_key="k", script=()) -> tuple[int, str, ScriptedJev]:
        out = io.StringIO()
        jev = ScriptedJev(script)
        jev.api_key = jev_api_key
        with (
            contextlib.redirect_stdout(out),
            patch("src.evaluation.__main__.Settings") as settings,
            patch("src.evaluation.__main__.JevClassifier", return_value=jev),
        ):
            settings.return_value = Settings(
                _env_file=None, db_path=self.db_path, jev_api_key=jev_api_key
            )
            code = main(["lab", *argv])
        return code, out.getvalue(), jev

    def test_needs_a_configured_jev(self):
        code, output, jev = self.run_cli(jev_api_key="")

        self.assertEqual(code, 2)
        self.assertIn("not configured", output)
        self.assertEqual(jev.requests, [])

    def test_unknown_variant_is_refused_with_the_available_ones(self):
        code, output, _ = self.run_cli("--variants", "current,telepathy")

        self.assertEqual(code, 2)
        self.assertIn("telepathy", output)
        self.assertIn("direct-action", output)

    def test_refuses_to_exceed_the_call_budget_before_any_call(self):
        code, output, jev = self.run_cli("--repeats", "3", "--max-calls", "100")

        self.assertEqual(code, 2)
        self.assertIn("450 JEV call(s)", output)
        self.assertIn("Refused", output)
        self.assertEqual(jev.requests, [])

    def test_runs_the_chosen_variant_on_the_corpus(self):
        code, output, jev = self.run_cli(
            "--variants", "current", "--limit", "2", script=[answers(), answers()]
        )

        self.assertEqual(code, 0)
        self.assertIn("2 JEV call(s): 1 variant(s) x 2 mail(s) x 1 run(s)", output)
        self.assertIn("current  2/2", output)
        self.assertEqual(len(jev.requests), 2)
        self.assertNotIn("NOT CONCLUSIVE", output)

    def test_rated_source_replays_real_verdicts_and_warns_on_a_small_sample(self):
        store = SqliteDecisionStore(self.db_path)
        store.record_decision(email("m1"), TriageResult("low", "personnel", 0.6, "jev"), "label")
        store.record_feedback("m1", "valid")
        store.close()
        mail = FakeMail(unread=[email("m1")])

        with patch.dict("src.evaluation.__main__.MAIL_PROVIDERS", {"gmail": lambda ctx: mail}):
            code, output, _ = self.run_cli(
                "--source", "rated", "--variants", "current", script=[answers()]
            )

        self.assertEqual(code, 0)
        self.assertIn("current  1/1", output)
        self.assertIn("Rated mails: 1, of which 0 decided by a rule", output)
        self.assertIn("NOT CONCLUSIVE", output)


if __name__ == "__main__":
    unittest.main()
