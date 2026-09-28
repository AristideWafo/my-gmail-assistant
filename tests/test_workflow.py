import unittest
from unittest.mock import patch

from src.gmail.client import EmailMessage
from src.llm.gemini import LLMAnalysis
from src.triage.engine import DecisionEngineClient, TriageResult
from src.workflow import EmailWorkflow


class FakeGemini:
    def __init__(self):
        self.calls = []

    def analyze(self, email, want_draft, want_entities):
        self.calls.append((want_draft, want_entities))
        return LLMAnalysis(
            summary=f"summary for {email.subject}",
            draft=f"draft for {email.subject}" if want_draft else "",
            entities={"poste": "DevOps", "entreprise": "Acme"} if want_entities else {},
        )


class FailingGemini:
    def analyze(self, email, want_draft, want_entities):
        raise RuntimeError("quota exhausted")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = EmailWorkflow(DecisionEngineClient(api_url=""), FakeGemini())

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

    def test_low_urgency_notification_systeme_routes_to_reject(self):
        workflow = EmailWorkflow(DecisionEngineClient(api_url="https://jev.example/triage", api_key="k"), FakeGemini())
        email = EmailMessage(
            id="6",
            thread_id="t6",
            sender="noreply@service.example",
            subject="Your receipt",
            snippet="Payment confirmed",
            body="",
        )

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "answers": {
                        "urgency": {"choice": "low", "confidence": 0.9},
                        "category": {"choice": "notification_systeme", "confidence": 0.9},
                    }
                }

        with patch("src.triage.engine.requests.post", return_value=FakeResponse()):
            result = workflow.run(email)

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
        workflow = EmailWorkflow(DecisionEngineClient(api_url="https://jev.example/triage", api_key="k"), FakeGemini())
        email = EmailMessage(
            id="5",
            thread_id="t5",
            sender="recruiter@example.com",
            subject="Job offer",
            snippet="We'd like to interview you",
            body="",
        )

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "answers": {
                        "urgency": {"choice": "high", "confidence": 0.9},
                        "category": {"choice": "offre_emploi", "confidence": 0.9},
                    }
                }

        with patch("src.triage.engine.requests.post", return_value=FakeResponse()):
            result = workflow.run(email)

        self.assertEqual(result["triage"].category, "offre_emploi")
        self.assertEqual(result["route"], "llm")
        self.assertEqual(result["entities"], {"poste": "DevOps", "entreprise": "Acme"})

    def test_llm_failure_still_routes_to_llm_with_snippet_fallback(self):
        workflow = EmailWorkflow(DecisionEngineClient(api_url=""), FailingGemini())
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
        gemini = FakeGemini()
        state = {
            "email": EmailMessage(id="8", thread_id="t8", sender="notifications@github.com", subject="s", snippet="n", body=""),
            "triage": TriageResult("high", "personnel", 0.9),
        }

        result = EmailWorkflow(DecisionEngineClient(api_url=""), gemini)._llm_node(state)

        self.assertEqual(gemini.calls, [(False, False)])
        self.assertNotIn("draft", result)

    def test_no_draft_requested_for_non_draftable_category(self):
        gemini = FakeGemini()
        state = {"email": EmailMessage(id="9", thread_id="t9", sender="p@x.io", subject="s", snippet="n", body="")}
        state["triage"] = TriageResult("high", "notification_systeme", 0.9)

        EmailWorkflow(DecisionEngineClient(api_url=""), gemini)._llm_node(state)

        self.assertEqual(gemini.calls, [(False, False)])


if __name__ == "__main__":
    unittest.main()
