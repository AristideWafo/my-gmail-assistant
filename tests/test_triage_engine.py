import unittest
from unittest.mock import patch

import requests

from src.gmail.client import EmailMessage
from src.observability.metrics import Metrics
from src.triage.engine import DecisionEngineClient


class DecisionEngineFallbackTests(unittest.TestCase):
    def setUp(self):
        self.email = EmailMessage(
            id="1",
            thread_id="t1",
            sender="person@example.com",
            subject="Urgent: please respond",
            snippet="Need this immediately",
            body="",
        )

    def test_classify_falls_back_and_counts_metric_on_request_exception(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage")
        before = Metrics.jev_fallback._value.get()

        with patch("src.triage.engine.requests.post", side_effect=requests.RequestException("boom")):
            result = client.classify(self.email)

        self.assertEqual(result.urgency, "high")
        self.assertEqual(Metrics.jev_fallback._value.get(), before + 1)

    def test_classify_uses_jev_response_when_available(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage")

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"urgency": "medium", "category": "offer", "confidence": 0.9}

        with patch("src.triage.engine.requests.post", return_value=FakeResponse()):
            result = client.classify(self.email)

        self.assertEqual(result.urgency, "medium")
        self.assertEqual(result.category, "offer")
        self.assertEqual(result.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
