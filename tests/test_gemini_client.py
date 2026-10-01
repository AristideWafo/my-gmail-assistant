import json
import unittest
from unittest.mock import MagicMock, patch

from google.api_core.exceptions import ResourceExhausted

from src.domain import EmailMessage
from src.llm.gemini import NO_COMMITMENT_RULE, GeminiClient
from src.llm.rate_limit import RateLimiter
from src.observability.metrics import Metrics


def fake_response(text: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> MagicMock:
    response = MagicMock(text=text)
    response.usage_metadata = MagicMock(prompt_token_count=prompt_tokens, candidates_token_count=completion_tokens)
    return response


def make_client(**kwargs) -> GeminiClient:
    with patch("src.llm.gemini.genai"):
        client = GeminiClient(api_key="fake-key", sleep=kwargs.pop("sleep", lambda _: None), **kwargs)
    client._model = MagicMock()
    return client


def make_email() -> EmailMessage:
    return EmailMessage(
        id="1",
        thread_id="t1",
        sender="recruiter@example.com",
        subject="Job offer",
        snippet="We'd like to interview you",
        body="Senior DevOps role, stack: AWS/Terraform, salary 70k, next step is a call",
    )


class GeminiAnalyzeTests(unittest.TestCase):
    def test_analyze_returns_summary_draft_and_entities_from_one_call(self):
        client = make_client()
        client._model.generate_content.return_value = fake_response(
            json.dumps({"summary": "• point", "draft": "Bonjour,\nMerci.", "entities": {"poste": "DevOps"}})
        )

        result = client.analyze(make_email(), want_draft=True, want_entities=True)

        self.assertEqual(client._model.generate_content.call_count, 1)
        self.assertEqual((result.summary, result.draft, result.entities), ("• point", "Bonjour,\nMerci.", {"poste": "DevOps"}))

    def test_analyze_cleans_chat_preamble_subject_and_markdown_from_draft(self):
        client = make_client()
        draft = "Voici une proposition de réponse courte et professionnelle :\n\n***\n\n**Objet :** RE: Hello\n\nBonjour Céline,\n\nÀ demain,\nAristide"
        client._model.generate_content.return_value = fake_response(json.dumps({"summary": "s", "draft": draft}))

        result = client.analyze(make_email(), want_draft=True, want_entities=False)

        self.assertEqual(result.draft, "Bonjour Céline,\n\nÀ demain,\nAristide")

    def test_analyze_strips_markdown_from_summary(self):
        client = make_client()
        client._model.generate_content.return_value = fake_response(
            json.dumps({"summary": "- **Event:** run failed\n- [logs](https://x.io/1)"})
        )

        result = client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(result.summary, "• Event: run failed\n• logs (https://x.io/1)")

    def test_analyze_ignores_draft_and_entities_when_not_requested(self):
        client = make_client()
        client._model.generate_content.return_value = fake_response(
            json.dumps({"summary": "s", "draft": "Bonjour", "entities": {"poste": "x"}})
        )

        result = client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual((result.draft, result.entities), ("", {}))

    def test_analyze_returns_empty_analysis_on_invalid_json(self):
        client = make_client()
        client._model.generate_content.return_value = fake_response("not json at all")
        before = Metrics.llm_errors.labels(reason="parse")._value.get()

        result = client.analyze(make_email(), want_draft=True, want_entities=True)

        self.assertEqual((result.summary, result.draft, result.entities), ("", "", {}))
        self.assertEqual(Metrics.llm_errors.labels(reason="parse")._value.get(), before + 1)

    def test_draft_with_bracket_placeholder_is_dropped(self):
        client = make_client()
        client._model.generate_content.return_value = fake_response(
            json.dumps({"summary": "s", "draft": "Bonjour,\n\nCordialement,\n[Your Name]"})
        )
        before = Metrics.llm_errors.labels(reason="placeholder")._value.get()

        result = client.analyze(make_email(), want_draft=True, want_entities=False)

        self.assertEqual((result.summary, result.draft), ("s", ""))
        self.assertEqual(Metrics.llm_errors.labels(reason="placeholder")._value.get(), before + 1)

    def test_analyze_disabled_without_api_key(self):
        result = GeminiClient(api_key="").analyze(make_email(), want_draft=True, want_entities=True)
        self.assertEqual((result.summary, result.draft, result.entities), ("", "", {}))

    def test_prompt_forbids_preamble_subject_placeholders_and_names_the_signature(self):
        client = make_client(user_name="Aristide")

        prompt = client._build_prompt(make_email(), want_draft=True, want_entities=False)

        self.assertIn("aucune introduction", prompt)
        self.assertIn("aucun objet", prompt)
        self.assertIn("entre crochets", prompt)
        self.assertIn('la signature "Aristide"', prompt)

    def test_draft_prompt_forbids_deciding_for_its_author(self):
        client = make_client()

        with_draft = client._build_prompt(make_email(), want_draft=True, want_entities=False)
        without = client._build_prompt(make_email(), want_draft=False, want_entities=False)

        self.assertIn(NO_COMMITMENT_RULE, with_draft)
        self.assertNotIn(NO_COMMITMENT_RULE, without)

    def test_prompt_only_asks_for_requested_keys(self):
        prompt = make_client()._build_prompt(make_email(), want_draft=False, want_entities=False)

        self.assertIn("clés: summary.", prompt)
        self.assertNotIn('"draft"', prompt)
        self.assertNotIn('"entities"', prompt)


class GeminiRetryTests(unittest.TestCase):
    def quota_error(self, seconds: int = 22) -> ResourceExhausted:
        return ResourceExhausted(f"429 quota exceeded. Please retry in {seconds}.683934226s.")

    def test_retries_after_the_delay_requested_by_the_api(self):
        sleeps = []
        client = make_client(sleep=sleeps.append)
        client._model.generate_content.side_effect = [self.quota_error(), fake_response('{"summary": "ok"}')]

        result = client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(result.summary, "ok")
        self.assertEqual(sleeps, [22.683934226])

    def test_retry_delay_is_capped(self):
        sleeps = []
        client = make_client(sleep=sleeps.append)
        client._model.generate_content.side_effect = [self.quota_error(500), fake_response('{"summary": "ok"}')]

        client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(sleeps, [60.0])

    def test_raises_after_exhausting_retries_and_counts_rate_limits(self):
        client = make_client()
        client._model.generate_content.side_effect = self.quota_error()
        before = Metrics.llm_errors.labels(reason="rate_limited")._value.get()

        with self.assertRaises(ResourceExhausted):
            client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(client._model.generate_content.call_count, 3)
        self.assertEqual(Metrics.llm_errors.labels(reason="rate_limited")._value.get(), before + 3)

    def test_every_attempt_goes_through_the_rate_limiter(self):
        limiter = MagicMock()
        client = make_client(limiter=limiter)
        client._model.generate_content.side_effect = [self.quota_error(), fake_response('{"summary": "ok"}')]

        client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(limiter.acquire.call_count, 2)


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += seconds

        self.limiter = RateLimiter(max_calls=2, window_seconds=60, clock=lambda: self.now, sleep=sleep)

    def test_calls_under_the_limit_do_not_wait(self):
        self.limiter.acquire()
        self.limiter.acquire()
        self.assertEqual(self.sleeps, [])

    def test_call_over_the_limit_waits_for_the_oldest_to_leave_the_window(self):
        self.limiter.acquire()
        self.now = 10
        self.limiter.acquire()

        self.limiter.acquire()

        self.assertEqual(self.sleeps, [50])

    def test_window_slides(self):
        self.limiter.acquire()
        self.limiter.acquire()
        self.now = 61
        self.limiter.acquire()
        self.assertEqual(self.sleeps, [])


class GeminiUsageTrackingTests(unittest.TestCase):
    def test_analyze_records_real_token_counts_and_cost_for_priced_model(self):
        client = make_client(model_name="gemini-2.5-flash")
        client._model.generate_content.return_value = fake_response(
            '{"summary": "s"}', prompt_tokens=1_000_000, completion_tokens=1_000_000
        )
        before_tokens = Metrics.llm_tokens.labels(kind="analysis", token_type="prompt")._value.get()
        before_cost = Metrics.llm_cost_usd.labels(kind="analysis")._value.get()

        client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(
            Metrics.llm_tokens.labels(kind="analysis", token_type="prompt")._value.get(), before_tokens + 1_000_000
        )
        self.assertAlmostEqual(Metrics.llm_cost_usd.labels(kind="analysis")._value.get(), before_cost + 0.30 + 2.50)

    def test_unknown_model_records_tokens_without_cost(self):
        client = make_client(model_name="some-future-model")
        client._model.generate_content.return_value = fake_response('{"summary": "s"}')
        before_cost = Metrics.llm_cost_usd.labels(kind="analysis")._value.get()

        client.analyze(make_email(), want_draft=False, want_entities=False)

        self.assertEqual(Metrics.llm_cost_usd.labels(kind="analysis")._value.get(), before_cost)


if __name__ == "__main__":
    unittest.main()
