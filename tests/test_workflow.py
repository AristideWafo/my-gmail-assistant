import unittest

from src.domain import EmailMessage, LLMAnalysis, TriageResult
from src.ports import EmailAnalyzer, EmailClassifier
from src.triage import HeuristicClassifier
from src.workflow import EmailWorkflow, expects_reply


class FixedClassifier:
    def __init__(self, result: TriageResult):
        self.result = result
        self.calls = []

    @property
    def is_configured(self) -> bool:
        return True

    def check_connection(self) -> str:
        return "fixed"

    def classify(self, email: EmailMessage) -> TriageResult:
        self.calls.append(email)
        return self.result


class FakeAnalyzer:
    def __init__(self):
        self.calls = []

    @property
    def is_configured(self) -> bool:
        return True

    def check_connection(self) -> str:
        return "fake"

    def analyze(self, email, want_draft, want_entities):
        self.calls.append((want_draft, want_entities))
        return LLMAnalysis(
            summary=f"summary for {email.subject}",
            draft=f"draft for {email.subject}" if want_draft else "",
            entities={"poste": "DevOps", "entreprise": "Acme"} if want_entities else {},
        )


class FailingAnalyzer(FakeAnalyzer):
    def analyze(self, email, want_draft, want_entities):
        raise RuntimeError("quota exhausted")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = EmailWorkflow(HeuristicClassifier(), FakeAnalyzer())

    def test_low_urgency_personnel_routes_to_label_not_reject(self):
        email = EmailMessage(
            id="1",
            thread_id="t1",
            sender="person@example.com",
            subject="General update",
            snippet="FYI the meeting notes are attached",
            body="",
        )
        result = self.workflow.run(email)
        self.assertEqual(result["triage"].urgency, "low")
        self.assertEqual(result["triage"].category, "personnel")
        self.assertEqual(result["route"], "label")
        self.assertNotIn("summary", result)
        self.assertNotIn("draft", result)

    def test_fakes_implement_the_ports(self):
        self.assertIsInstance(FixedClassifier(TriageResult("low", "personnel", 0.9)), EmailClassifier)
        self.assertIsInstance(FakeAnalyzer(), EmailAnalyzer)

    def test_low_urgency_notification_systeme_routes_to_reject(self):
        classifier = FixedClassifier(TriageResult("low", "notification_systeme", 0.9))
        workflow = EmailWorkflow(classifier, FakeAnalyzer())
        email = EmailMessage(
            id="6",
            thread_id="t6",
            sender="noreply@service.example",
            subject="Your receipt",
            snippet="Payment confirmed",
            body="",
        )

        result = workflow.run(email)

        self.assertEqual(classifier.calls, [email])
        self.assertEqual(result["route"], "reject")

    def test_newsletter_sender_routes_to_reject_regardless_of_urgency(self):
        email = EmailMessage(
            id="4",
            thread_id="t4",
            sender="newsletter@brand.com",
            subject="This week's digest",
            snippet="Check out our latest deals, click here to unsubscribe",
            body="",
        )
        result = self.workflow.run(email)
        self.assertEqual(result["triage"].category, "newsletter")
        self.assertEqual(result["route"], "reject")

    def test_offer_routes_to_label(self):
        email = EmailMessage(
            id="2",
            thread_id="t2",
            sender="recruiter@example.com",
            subject="Job offer",
            snippet="We'd like to interview you for the position",
            body="",
        )
        result = self.workflow.run(email)
        self.assertEqual(result["triage"].urgency, "medium")
        self.assertEqual(result["route"], "label")
        self.assertNotIn("summary", result)
        self.assertNotIn("draft", result)

    def test_urgent_calls_llm(self):
        email = EmailMessage(
            id="3",
            thread_id="t3",
            sender="person@example.com",
            subject="Urgent: please respond",
            snippet="Need this immediately",
            body="",
        )
        result = self.workflow.run(email)
        self.assertEqual(result["triage"].urgency, "high")
        self.assertEqual(result["route"], "llm")
        self.assertIn("summary", result)
        self.assertIn("draft", result)
        self.assertNotIn("entities", result)

    def test_urgent_offer_extracts_entities(self):
        analyzer = FakeAnalyzer()
        workflow = EmailWorkflow(FixedClassifier(TriageResult("high", "offre_emploi", 0.9)), analyzer)
        email = EmailMessage(
            id="5",
            thread_id="t5",
            sender="recruiter@example.com",
            subject="Job offer",
            snippet="We'd like to interview you",
            body="",
        )

        result = workflow.run(email)

        self.assertEqual(result["triage"].category, "offre_emploi")
        self.assertEqual(result["route"], "llm")
        self.assertEqual(result["entities"], {"poste": "DevOps", "entreprise": "Acme"})
        self.assertEqual(analyzer.calls, [(True, True)])

    def test_llm_failure_still_routes_to_llm_with_snippet_fallback(self):
        workflow = EmailWorkflow(HeuristicClassifier(), FailingAnalyzer())
        email = EmailMessage(
            id="7",
            thread_id="t7",
            sender="person@example.com",
            subject="Urgent: please respond",
            snippet="Need this immediately",
            body="",
        )

        result = workflow.run(email)

        self.assertEqual(result["route"], "llm")
        self.assertIn("Need this immediately", result["summary"])
        self.assertIn("(résumé indisponible)", result["summary"])
        self.assertNotIn("draft", result)

    def test_no_draft_requested_for_automated_sender(self):
        analyzer = FakeAnalyzer()
        state = {
            "email": EmailMessage(id="8", thread_id="t8", sender="notifications@github.com", subject="s", snippet="n", body=""),
            "triage": TriageResult("high", "personnel", 0.9),
        }

        result = EmailWorkflow(HeuristicClassifier(), analyzer)._llm_node(state)

        self.assertEqual(analyzer.calls, [(False, False)])
        self.assertNotIn("draft", result)

    def test_no_draft_requested_for_non_draftable_category(self):
        analyzer = FakeAnalyzer()
        state = {"email": EmailMessage(id="9", thread_id="t9", sender="p@x.io", subject="s", snippet="n", body="")}
        state["triage"] = TriageResult("high", "notification_systeme", 0.9)

        EmailWorkflow(HeuristicClassifier(), analyzer)._llm_node(state)

        self.assertEqual(analyzer.calls, [(False, False)])


def reply_email(sender="marc.dupont@gmail.com"):
    return EmailMessage(id="r1", thread_id="t1", sender=sender, subject="Le livre", snippet="s", body="b")


class ExpectsReplyTests(unittest.TestCase):
    def test_decision_table(self):
        cases = [
            ("above threshold", "personnel", 0.9, 0.5, "marc@gmail.com", True),
            ("at threshold", "personnel", 0.5, 0.5, "marc@gmail.com", True),
            ("below threshold", "personnel", 0.49, 0.5, "marc@gmail.com", False),
            ("question not asked", "personnel", None, 0.5, "marc@gmail.com", False),
            ("feature off", "personnel", 0.9, None, "marc@gmail.com", False),
            ("administration", "notification_systeme", 0.9, 0.5, "c.roche@dgfip.gouv.fr", True),
            ("scam asking for a reply", "spam", 0.99, 0.5, "ibrahim@consultant.com", False),
            ("newsletter bait", "newsletter", 0.93, 0.5, "team@lewagon.com", False),
            ("promotion bait", "promotion", 0.83, 0.5, "promo@cdiscount.com", False),
            ("automated sender", "alerte_technique", 0.9, 0.5, "notifications@github.com", False),
        ]
        for name, category, needs_reply, threshold, sender, expected in cases:
            with self.subTest(name):
                triage = TriageResult("medium", category, 0.9, "jev", needs_reply)

                self.assertEqual(expects_reply(reply_email(sender), triage, threshold), expected)


class ReplyDraftTests(unittest.TestCase):
    def run_workflow(self, triage, analyzer=None, threshold=0.5):
        self.analyzer = analyzer or FakeAnalyzer()
        workflow = EmailWorkflow(FixedClassifier(triage), self.analyzer, needs_reply_threshold=threshold)
        return workflow.run(reply_email())

    def test_non_urgent_mail_expecting_a_reply_gets_a_draft_and_keeps_its_route(self):
        result = self.run_workflow(TriageResult("low", "personnel", 0.9, "jev", 0.93))

        self.assertEqual(result["route"], "label")
        self.assertTrue(result["reply_expected"])
        self.assertEqual(result["draft"], "draft for Le livre")
        self.assertEqual(self.analyzer.calls, [(True, False)])
        self.assertNotIn("summary", result)

    def test_analyzer_failure_keeps_the_flag_without_a_draft(self):
        result = self.run_workflow(
            TriageResult("low", "personnel", 0.9, "jev", 0.93), analyzer=FailingAnalyzer()
        )

        self.assertEqual(result["route"], "label")
        self.assertTrue(result["reply_expected"])
        self.assertNotIn("draft", result)

    def test_nothing_changes_when_no_reply_is_expected(self):
        for triage, threshold in (
            (TriageResult("low", "personnel", 0.9, "jev", 0.1), 0.5),
            (TriageResult("low", "personnel", 0.9, "jev", 0.93), None),
            (TriageResult("low", "personnel", 0.9, "heuristic"), 0.5),
        ):
            with self.subTest(needs_reply=triage.needs_reply, threshold=threshold):
                result = self.run_workflow(triage, threshold=threshold)

                self.assertEqual(result["route"], "label")
                self.assertNotIn("reply_expected", result)
                self.assertNotIn("draft", result)
                self.assertEqual(self.analyzer.calls, [])

    def test_archived_mail_is_never_drafted(self):
        result = self.run_workflow(TriageResult("low", "newsletter", 0.9, "jev", 0.93))

        self.assertEqual(result["route"], "reject")
        self.assertEqual(self.analyzer.calls, [])

    def test_urgent_mail_keeps_the_alert_path(self):
        result = self.run_workflow(TriageResult("high", "personnel", 0.9, "jev", 0.93))

        self.assertEqual(result["route"], "llm")
        self.assertNotIn("reply_expected", result)
        self.assertEqual(self.analyzer.calls, [(True, False)])


if __name__ == "__main__":
    unittest.main()
