import unittest
from unittest.mock import MagicMock, patch

from src.gmail.client import EmailMessage
from src.llm.gemini import GeminiClient


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
            client._model.generate_content.return_value = MagicMock(
                text='{"poste": "DevOps", "entreprise": "Acme", "stack": "AWS", '
                '"salaire": "70k", "prochaine_etape": "call"}'
            )

            result = client.extract_job_entities(self.email)

        self.assertEqual(result["poste"], "DevOps")
        self.assertEqual(result["entreprise"], "Acme")

    def test_extract_job_entities_returns_empty_dict_on_invalid_json(self):
        with patch("src.llm.gemini.genai"):
            client = GeminiClient(api_key="fake-key")
            client._model = MagicMock()
            client._model.generate_content.return_value = MagicMock(text="not json at all")

            result = client.extract_job_entities(self.email)

        self.assertEqual(result, {})

    def test_extract_job_entities_disabled_without_api_key(self):
        client = GeminiClient(api_key="")
        self.assertEqual(client.extract_job_entities(self.email), {})


if __name__ == "__main__":
    unittest.main()
