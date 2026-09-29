import unittest

from src.domain import EmailMessage, LLMAnalysis, TriageResult
from src.ports import EmailAnalyzer, EmailClassifier
from src.triage import HeuristicClassifier
from src.workflow import EmailWorkflow


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


if __name__ == "__main__":
    unittest.main()
