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
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="k")
        before = Metrics.jev_fallback._value.get()

        with patch("src.triage.engine.requests.post", side_effect=requests.RequestException("boom")):
            result = client.classify(self.email)

        self.assertEqual(result.urgency, "high")
        self.assertEqual(result.category, "personnel")
        self.assertEqual(Metrics.jev_fallback._value.get(), before + 1)

    def test_fallback_detects_newsletter_by_sender(self):
        client = DecisionEngineClient(api_url="")
        email = EmailMessage(
            id="2",
            thread_id="t2",
            sender="newsletter@brand.com",
            subject="This week's digest",
            snippet="Great deals inside, click to unsubscribe",
            body="",
        )

        result = client.classify(email)

        self.assertEqual(result.category, "newsletter")
        self.assertEqual(result.urgency, "low")

    @staticmethod
    def _jev_response(urgency, category, urgency_conf=0.9, category_conf=0.8):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "answers": {
                        "urgency": {"type": "choice", "choice": urgency, "confidence": urgency_conf},
                        "category": {"type": "choice", "choice": category, "confidence": category_conf},
                    }
                }

        return FakeResponse()

    def test_classify_maps_jev_answers_and_takes_min_confidence(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="k")

        with patch("src.triage.engine.requests.post", return_value=self._jev_response("medium", "offre_emploi")):
            result = client.classify(self.email)

        self.assertEqual((result.urgency, result.category, result.confidence), ("medium", "offre_emploi", 0.8))

    def test_classify_accepts_spam_category(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="k")

        with patch("src.triage.engine.requests.post", return_value=self._jev_response("low", "spam")):
            result = client.classify(self.email)

        self.assertEqual(result.category, "spam")

    def test_classify_accepts_notification_systeme_and_mise_en_relation(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="k")

        with patch("src.triage.engine.requests.post", return_value=self._jev_response("low", "notification_systeme")):
            result = client.classify(self.email)
        self.assertEqual(result.category, "notification_systeme")

        with patch("src.triage.engine.requests.post", return_value=self._jev_response("medium", "mise_en_relation")):
            result = client.classify(self.email)
        self.assertEqual(result.category, "mise_en_relation")

    def test_classify_sends_bearer_key_and_typed_questions(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="secret")

        with patch("src.triage.engine.requests.post", return_value=self._jev_response("low", "general")) as post:
            client.classify(self.email)

        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer secret"})
        self.assertEqual(kwargs["json"]["model"], "jev-latest")
        self.assertEqual(kwargs["json"]["state"]["subject"], self.email.subject)
        self.assertEqual(set(kwargs["json"]["questions"]), {"urgency", "category"})
        self.assertEqual(
            set(kwargs["json"]["questions"]["category"]["criteria"]),
            {"offre_emploi", "mise_en_relation", "newsletter", "notification_systeme", "personnel", "spam"},
        )

    def test_classify_skips_api_without_key(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="")

        with patch("src.triage.engine.requests.post") as post:
            result = client.classify(self.email)

        post.assert_not_called()
        self.assertEqual(result.urgency, "high")

    def test_classify_falls_back_on_malformed_response(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="k")
        before = Metrics.jev_fallback._value.get()

        class BadResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"answers": {}}

        with patch("src.triage.engine.requests.post", return_value=BadResponse()):
            result = client.classify(self.email)

        self.assertEqual(result.urgency, "high")
        self.assertEqual(Metrics.jev_fallback._value.get(), before + 1)


if __name__ == "__main__":
    unittest.main()
