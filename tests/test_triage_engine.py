import json
import unittest
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import patch

import requests

from src.domain import EmailMessage
from src.observability.metrics import Metrics
from src.triage.engine import CATEGORIES, URGENCIES, URGENCY_INSTRUCTIONS, DecisionEngineClient
from src.triage.few_shot import FEW_SHOT_INSTRUCTION


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
            {
                "offre_emploi",
                "alerte_emploi",
                "mise_en_relation",
                "newsletter",
                "promotion",
                "alerte_technique",
                "notification_systeme",
                "personnel",
                "spam",
            },
        )

    def test_classify_sends_dates_and_a_strict_urgency_definition(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="secret")
        self.email.received_at = "2026-09-25"

        with patch("src.triage.engine.requests.post", return_value=self._jev_response("low", "personnel")) as post:
            client.classify(self.email)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["state"]["received_at"], "2026-09-25")
        self.assertEqual(payload["state"]["today"], datetime.now(UTC).date().isoformat())
        self.assertIn("never high", payload["questions"]["urgency"]["instructions"])

    def test_classify_accepts_new_categories(self):
        client = DecisionEngineClient(api_url="https://jev.example/triage", api_key="k")
        for category in ("alerte_emploi", "promotion", "alerte_technique"):
            with (
                self.subTest(category=category),
                patch("src.triage.engine.requests.post", return_value=self._jev_response("low", category)),
            ):
                self.assertEqual(client.classify(self.email).category, category)

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


class FewShotRequestTests(unittest.TestCase):
    EXAMPLES: ClassVar[list[dict[str, str]]] = [
        {
            "sender_domain": "corp.com",
            "subject": "Quarterly report",
            "wrong_urgency": "high",
            "wrong_category": "personnel",
            "correct_urgency": "medium",
            "correct_category": "personnel",
        }
    ]

    def setUp(self):
        self.email = EmailMessage(
            id="1",
            thread_id="t1",
            sender="person@example.com",
            subject="Hello",
            snippet="snippet",
            body="body",
            received_at="2026-09-25",
        )

    def _client(self, provider=None):
        return DecisionEngineClient(
            "https://jev.example/triage", api_key="k", examples_provider=provider
        )

    def test_request_without_provider_is_unchanged(self):
        expected = {
            "model": "jev-latest",
            "state": {
                "subject": "Hello",
                "body": "body",
                "sender": "person@example.com",
                "received_at": "2026-09-25",
                "today": datetime.now(UTC).date().isoformat(),
            },
            "questions": {
                "urgency": {
                    "type": "choice",
                    "instructions": URGENCY_INSTRUCTIONS,
                    "criteria": URGENCIES,
                },
                "category": {
                    "type": "choice",
                    "instructions": "Which category best describes this email?",
                    "criteria": CATEGORIES,
                },
            },
        }

        request = self._client()._build_request(self.email)

        self.assertEqual(json.dumps(request), json.dumps(expected))

    def test_empty_examples_leave_request_unchanged(self):
        self.assertEqual(
            self._client(list)._build_request(self.email),
            self._client()._build_request(self.email),
        )

    def test_examples_are_added_to_state_and_both_instructions(self):
        request = self._client(lambda: self.EXAMPLES)._build_request(self.email)

        self.assertEqual(request["state"]["examples"], self.EXAMPLES)
        self.assertEqual(
            request["questions"]["urgency"]["instructions"],
            f"{URGENCY_INSTRUCTIONS} {FEW_SHOT_INSTRUCTION}",
        )
        self.assertEqual(
            request["questions"]["category"]["instructions"],
            f"Which category best describes this email? {FEW_SHOT_INSTRUCTION}",
        )
        self.assertNotIn(FEW_SHOT_INSTRUCTION, URGENCY_INSTRUCTIONS)

    def test_classify_sends_examples_to_jev(self):
        client = self._client(lambda: self.EXAMPLES)
        response = DecisionEngineFallbackTests._jev_response("medium", "personnel")

        with patch("src.triage.engine.requests.post", return_value=response) as post:
            result = client.classify(self.email)

        self.assertEqual(post.call_args.kwargs["json"]["state"]["examples"], self.EXAMPLES)
        self.assertEqual(result.urgency, "medium")

    def test_provider_failure_classifies_without_examples(self):
        def broken():
            raise RuntimeError("store unavailable")

        client = self._client(broken)
        response = DecisionEngineFallbackTests._jev_response("low", "newsletter")
        before = Metrics.jev_fallback._value.get()

        with (
            self.assertLogs("src.triage.engine", level="WARNING") as logs,
            patch("src.triage.engine.requests.post", return_value=response) as post,
        ):
            result = client.classify(self.email)

        self.assertNotIn("examples", post.call_args.kwargs["json"]["state"])
        self.assertEqual((result.urgency, result.category), ("low", "newsletter"))
        self.assertEqual(Metrics.jev_fallback._value.get(), before)
        self.assertIn("store unavailable", logs.output[0])


if __name__ == "__main__":
    unittest.main()
