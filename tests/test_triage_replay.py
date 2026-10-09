import contextlib
import io
import os
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from src.agent import profiles
from src.agent.loop import Limits
from src.agent.profiles import AgentPorts
from src.agent.prompts import triage_prompt, triage_system
from src.agent.toolsets.triage import DECIDED, UNKNOWN_ROUTE, Decision, more_visible
from src.config import Settings
from src.domain import AgentTurn, EmailMessage, ToolCall, TriageResult, Usage
from src.evaluation.__main__ import main
from src.evaluation.dataset import Case
from src.evaluation.triage_replay import (
    ALONE,
    DETERMINISTIC,
    WITH_JEV,
    format_replay_report,
    judge,
    replay,
)
from src.storage import SqliteDecisionStore
from tests.agent_helpers import SECRET, world
from tests.fakes import FakeAgentModel, FakeMail

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
LIMITS = Limits(max_steps=3, max_tokens=10_000)
NEWSLETTER = TriageResult("low", "newsletter", 0.95, source="jev")
UNSURE = TriageResult("low", "notification_systeme", 0.6, source="jev")


def email(message_id, subject="Hello", thread_id="t1"):
    return EmailMessage(message_id, thread_id, "a@example.com", subject, "snippet", "body text")


def case(message_id, verdict, recorded_route, subject="Hello"):
    return Case(email(message_id, subject), verdict, recorded_route, "2026-10-01")


def deciding(route, tokens=100):
    call = ToolCall("decide", {"route": route, "reason": "parce que"})
    return AgentTurn(tool_calls=(call,), usage=Usage(tokens, 0, 0.001))


class VerdictsBySubject:
    is_configured = True

    def __init__(self, verdicts, failing=()):
        self.verdicts = verdicts
        self.failing = failing

    def classify(self, mail):
        if mail.id in self.failing:
            raise RuntimeError("jev down")
        return self.verdicts[mail.id]


class DecideToolTests(unittest.TestCase):
    def setUp(self):
        self.ports = world()
        self.addCleanup(self.ports.store.close)
        self.decision = Decision()
        self.box = profiles.triage_replay(self.ports, "t1", self.decision)

    def test_it_reads_its_thread_and_gives_an_opinion_and_nothing_more(self):
        self.assertEqual(
            {spec.name for spec in self.box.specs},
            {"read_mail", "read_thread", "ask_jev", "decide"},
        )

    def test_an_opinion_is_recorded_as_a_route_and_ends_the_run(self):
        for said, route in (("alert", "llm"), ("keep", "label"), ("archive", "reject")):
            with self.subTest(said=said):
                result = self.box.execute(
                    ToolCall("decide", {"route": said, "reason": "une\nraison"})
                )

                self.assertEqual((result.content, result.ends_run), (DECIDED, True))
                self.assertEqual((self.decision.route, self.decision.reason), (route, "une raison"))
        self.assertEqual(self.ports.mail.writes, [])

    def test_anything_else_is_not_a_route(self):
        result = self.box.execute(ToolCall("decide", {"route": "delete", "reason": "x"}))

        self.assertEqual(
            (result.content, result.is_error, result.ends_run), (UNKNOWN_ROUTE, True, False)
        )
        self.assertEqual(self.decision.route, "")

    def test_another_thread_stays_out_of_reach(self):
        result = self.box.execute(ToolCall("read_mail", {"message_id": "m9"}))

        self.assertTrue(result.is_error)
        self.assertNotIn(SECRET, result.content)

    def test_a_mail_without_a_thread_is_judged_without_reading_tools(self):
        box = profiles.triage_replay(self.ports, "", Decision())

        self.assertEqual({spec.name for spec in box.specs}, {"decide"})

    def test_the_agent_can_rescue_a_mail_and_never_bury_one(self):
        self.assertEqual(more_visible("llm", "reject"), "llm")
        self.assertEqual(more_visible("label", "reject"), "label")
        self.assertEqual(more_visible("reject", "label"), "label")
        self.assertEqual(more_visible("label", "llm"), "llm")
        self.assertEqual(more_visible("label", "label"), "label")


class PromptTests(unittest.TestCase):
    def test_the_mail_is_given_whole_and_jevs_verdict_only_when_shown(self):
        mail = email("m1", subject="Devis")

        shown = triage_prompt(mail, NEWSLETTER)
        hidden = triage_prompt(mail, None)

        self.assertIn("Objet : Devis", hidden)
        self.assertIn("body text", hidden)
        self.assertNotIn("JEV", hidden)
        self.assertIn("catégorie newsletter, confiance 0.95", shown)

    def test_the_system_says_what_each_route_is_and_what_mail_is(self):
        system = triage_system("Aristide", NOW.date())

        for word in ("alert", "keep", "archive", "jamais d'instruction", "Aristide", "2026-10-09"):
            self.assertIn(word, system)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.ports = AgentPorts(FakeMail(), self.store, clock=lambda: NOW)
        # m1: rightly archived. m2: wrongly archived, JEV unsure.
        self.cases = [case("m1", "valid", "reject"), case("m2", "wrong_archive", "reject")]
        self.jev = VerdictsBySubject({"m1": NEWSLETTER, "m2": UNSURE})

    def judged(self):
        return judge(self.cases, self.jev, 0.5)

    def replay(self, *script):
        self.model = FakeAgentModel(list(script))
        reports = replay(self.judged(), self.model, self.ports, LIMITS, "Aristide")
        return {report.name: report for report in reports}

    def cell(self, report, name):
        tally = report.by_slice[name]
        return (tally.passed, tally.total)

    def test_jev_and_the_code_give_todays_route_for_each_mail(self):
        first, second = self.judged()

        self.assertEqual((first.route, first.slices), ("reject", ["all"]))
        self.assertEqual(
            (second.route, second.slices), ("reject", ["all", "JEV unsure", "corrected by you"])
        )

    def test_a_mail_jev_cannot_judge_is_left_out(self):
        self.jev.failing = ("m1",)

        with self.assertLogs("src.evaluation.triage_replay", level="WARNING"):
            self.assertEqual([one.case.email.id for one in self.judged()], ["m2"])

    def test_each_mail_is_played_in_both_arms_and_scored_against_the_verdict(self):
        # m1 shown, m1 alone, m2 shown, m2 alone.
        reports = self.replay(
            deciding("archive"), deciding("keep"), deciding("archive"), deciding("keep")
        )

        self.assertEqual(self.cell(reports[DETERMINISTIC], "all"), (1, 2))
        self.assertEqual(self.cell(reports[WITH_JEV], "all"), (1, 2))
        self.assertEqual(self.cell(reports[ALONE], "all"), (1, 2))
        self.assertEqual(self.cell(reports[ALONE], "corrected by you"), (1, 1))
        self.assertEqual(self.cell(reports[WITH_JEV], "JEV unsure"), (0, 1))
        self.assertEqual((reports[WITH_JEV].differs, reports[ALONE].differs), (0, 2))
        self.assertEqual(reports[ALONE].usage.tokens, 200)
        shown, alone = (seen[1][0].text for seen in self.model.seen[:2])
        self.assertIn("JEV", shown)
        self.assertNotIn("JEV", alone)

    def test_never_less_visible_keeps_todays_route_when_the_agent_would_bury(self):
        self.cases = [case("m1", "valid", "label")]
        self.jev = VerdictsBySubject({"m1": TriageResult("medium", "personnel", 0.9)})

        reports = self.replay(deciding("archive"), deciding("alert"))

        self.assertEqual(self.cell(reports[WITH_JEV], "all"), (0, 1))
        self.assertEqual(self.cell(reports[f"{WITH_JEV}, never less visible"], "all"), (1, 1))
        self.assertEqual(reports[f"{WITH_JEV}, never less visible"].differs, 0)
        self.assertEqual(self.cell(reports[f"{ALONE}, never less visible"], "all"), (0, 1))
        self.assertEqual(reports[f"{ALONE}, never less visible"].differs, 1)

    def test_a_run_without_an_opinion_is_a_miss_as_given_and_todays_route_in_production(self):
        self.cases = self.cases[:1]

        reports = self.replay(AgentTurn(text="Je ne sais pas."), deciding("archive"))

        self.assertEqual(
            (reports[WITH_JEV].undecided, self.cell(reports[WITH_JEV], "all")), (1, (0, 1))
        )
        self.assertEqual(self.cell(reports[f"{WITH_JEV}, never less visible"], "all"), (1, 1))

    def test_nothing_is_written_anywhere(self):
        before = list(self.store._conn.iterdump())

        self.replay(deciding("alert"), deciding("alert"), deciding("alert"), deciding("alert"))

        self.assertEqual(list(self.store._conn.iterdump()), before)
        self.assertEqual((self.ports.mail.archived, self.ports.mail.labels), ([], []))

    def test_the_report_names_the_model_the_arms_and_the_caution(self):
        reports = self.replay(
            deciding("archive"), deciding("keep"), deciding("archive"), deciding("keep")
        )

        report = format_replay_report(list(reports.values()), 2, "gemini-2.5-flash")

        self.assertIn("Model: gemini-2.5-flash, 2 rated mail(s)", report)
        self.assertRegex(report, r"deterministic \(today\)\s+50% \(1/2\)\s+0% \(0/1\)\s+0% \(0/1\)")
        self.assertRegex(
            report, r"agent, not shown it\s+50% \(1/2\)\s+100% \(1/1\)\s+100% \(1/1\)\s+2"
        )
        self.assertIn("read the counts", report)


class AgentTriageCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "assistant.db")
        store = SqliteDecisionStore(self.db_path)
        for message_id, verdict in (("m1", "valid"), ("m2", "wrong_archive")):
            store.record_decision(email(message_id), NEWSLETTER, "reject")
            store.record_feedback(message_id, verdict, "review")
        store.close()
        self.mail = FakeMail(unread=[email("m1"), email("m2")])

    def run_cli(self, *argv, model=None, jev_api_key="k"):
        out = io.StringIO()
        settings = Settings(_env_file=None, db_path=self.db_path, jev_api_key=jev_api_key)
        jev = VerdictsBySubject({"m1": NEWSLETTER, "m2": UNSURE})
        jev.is_configured = bool(jev_api_key)
        with (
            contextlib.redirect_stdout(out),
            patch("src.evaluation.__main__.Settings", return_value=settings),
            patch("src.evaluation.__main__.JevClassifier", return_value=jev),
            patch.dict("src.evaluation.__main__.AGENT_MODELS", {"gemini": lambda ctx: model}),
            patch.dict("src.evaluation.__main__.MAIL_PROVIDERS", {"gmail": lambda ctx: self.mail}),
        ):
            code = main(["agent-triage", *argv])
        return code, out.getvalue()

    def test_needs_jev_and_a_configured_model(self):
        code, output = self.run_cli(model=FakeAgentModel(), jev_api_key="")
        self.assertEqual((code, "JEV is not configured" in output), (2, True))

        code, output = self.run_cli(model=FakeAgentModel(is_configured=False))
        self.assertEqual((code, "agent model is not configured" in output), (2, True))

    def test_refuses_to_run_past_the_call_budget(self):
        model = FakeAgentModel()

        code, output = self.run_cli("--max-calls", "3", model=model)

        self.assertEqual((code, "Refused" in output), (2, True))
        self.assertEqual(model.seen, [])

    def test_rated_mails_are_replayed_and_reported_without_touching_them(self):
        model = FakeAgentModel([deciding("archive"), deciding("keep")] * 2)

        code, output = self.run_cli(model=model)

        self.assertEqual(code, 0, output)
        self.assertIn("2 rated mail(s) not decided by a rule", output)
        self.assertRegex(output, r"agent, not shown it\s+50% \(1/2\)")
        self.assertEqual((self.mail.archived, self.mail.labels), ([], []))

    def test_limit_keeps_the_latest_verdicts(self):
        model = FakeAgentModel([deciding("keep")] * 2)

        code, output = self.run_cli("--limit", "1", model=model)

        self.assertEqual(code, 0, output)
        self.assertIn("1 rated mail(s)", output)


if __name__ == "__main__":
    unittest.main()
