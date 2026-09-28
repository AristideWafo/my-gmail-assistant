import unittest

from src.gmail.client import EmailMessage
from src.triage.engine import DecisionEngineClient
from src.workflow import EmailWorkflow


class FakeGemini:
    def summarize(self, email):
        return f"summary for {email.subject}"

    def draft_reply(self, email):
        return f"draft for {email.subject}"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = EmailWorkflow(DecisionEngineClient(api_url=""), FakeGemini())

    def test_non_urgent_general_routes_to_reject(self):
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
        self.assertEqual(result["route"], "reject")
        self.assertNotIn("summary", result)
        self.assertNotIn("draft", result)

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


if __name__ == "__main__":
    unittest.main()
