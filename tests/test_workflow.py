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

    def test_non_urgent_does_not_call_llm(self):
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
        self.assertNotIn("summary", result)
        self.assertNotIn("draft", result)

    def test_urgent_calls_llm(self):
        email = EmailMessage(
            id="2",
            thread_id="t2",
            sender="person@example.com",
            subject="Urgent: please respond",
            snippet="Need this immediately",
            body="",
        )
        result = self.workflow.run(email)
        self.assertEqual(result["triage"].urgency, "high")
        self.assertIn("summary", result)
        self.assertIn("draft", result)


if __name__ == "__main__":
    unittest.main()
