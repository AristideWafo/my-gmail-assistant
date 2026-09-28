import unittest
from unittest.mock import MagicMock, patch

from src.gmail.client import EmailMessage
from src.llm.gemini import GeminiClient
from src.observability.metrics import Metrics


def fake_response(text: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> MagicMock:
    response = MagicMock(text=text)
    response.usage_metadata = MagicMock(prompt_token_count=prompt_tokens, candidates_token_count=completion_tokens)
    return response


class GeminiJobEntityExtractionTests(unittest.TestCase):
    def setUp(self):
        self.email = EmailMessage(
            id="1",
            thread_id="t1",
            sender="recruiter@example.com",
            subject="Job offer",
            snippet="We'd like to interview you",
            body="Senior DevOps role, stack: AWS/Terraform, salary 70k, next step is a call",
        )

    def test_extract_job_entities_parses_valid_json(self):
        with patch("src.llm.gemini.genai"):
            client = GeminiClient(api_key="fake-key")
            client._model = MagicMock()
            client._model.generate_content.return_value = fake_response(
                '{"poste": "DevOps", "entreprise": "Acme", "stack": "AWS", '
                '"salaire": "70k", "prochaine_etape": "call"}'
            )

            result = client.extract_job_entities(self.email)

        self.assertEqual(result["poste"], "DevOps")
        self.assertEqual(result["entreprise"], "Acme")

    def test_extract_job_entities_returns_empty_dict_on_invalid_json(self):
        with patch("src.llm.gemini.genai"):
            client = GeminiClient(api_key="fake-key")
            client._model = MagicMock()
            client._model.generate_content.return_value = fake_response("not json at all")

            result = client.extract_job_entities(self.email)

        self.assertEqual(result, {})

    def test_extract_job_entities_disabled_without_api_key(self):
        client = GeminiClient(api_key="")
        self.assertEqual(client.extract_job_entities(self.email), {})


class GeminiUsageTrackingTests(unittest.TestCase):
    def setUp(self):
        self.email = EmailMessage(
            id="1", thread_id="t1", sender="a@b.com", subject="Hi", snippet="s", body="b"
        )

    def test_summarize_records_real_token_counts_and_cost_for_priced_model(self):
        with patch("src.llm.gemini.genai"):
            client = GeminiClient(api_key="fake-key", model_name="gemini-2.5-flash")
            client._model = MagicMock()
            client._model.generate_content.return_value = fake_response(
                "summary", prompt_tokens=1_000_000, completion_tokens=1_000_000
            )
            before_tokens = Metrics.llm_tokens.labels(kind="summary", token_type="prompt")._value.get()
            before_cost = Metrics.llm_cost_usd.labels(kind="summary")._value.get()

            client.summarize(self.email)

        self.assertEqual(
            Metrics.llm_tokens.labels(kind="summary", token_type="prompt")._value.get(), before_tokens + 1_000_000
        )
        self.assertAlmostEqual(Metrics.llm_cost_usd.labels(kind="summary")._value.get(), before_cost + 0.30 + 2.50)

    def test_unknown_model_records_tokens_without_cost(self):
        with patch("src.llm.gemini.genai"):
            client = GeminiClient(api_key="fake-key", model_name="some-future-model")
            client._model = MagicMock()
            client._model.generate_content.return_value = fake_response("draft")
            before_cost = Metrics.llm_cost_usd.labels(kind="draft")._value.get()

            client.draft_reply(self.email)

        self.assertEqual(Metrics.llm_cost_usd.labels(kind="draft")._value.get(), before_cost)


if __name__ == "__main__":
    unittest.main()
