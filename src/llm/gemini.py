import json
import logging

import google.generativeai as genai

from src.gmail.client import EmailMessage
from src.observability.metrics import Metrics

logger = logging.getLogger(__name__)

JOB_ENTITY_FIELDS = ("poste", "entreprise", "stack", "salaire", "prochaine_etape")

# $/1M tokens (input, output), verified on ai.google.dev/gemini-api/docs/pricing.
# Older models (1.5/2.0 flash) are retired and no longer listed there; add only prices you can verify.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
}
_unpriced_models_warned: set[str] = set()


class GeminiClient:
    def __init__(self, api_key: str, model_name: str = "gemini-1.5-flash") -> None:
        self.model_name = model_name
        self._enabled = bool(api_key)
        if self._enabled:
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(model_name)

    @property
    def is_configured(self) -> bool:
        return self._enabled

    def check_connection(self) -> str:
        genai.get_model(f"models/{self.model_name}")
        return f"model {self.model_name} available"

    def _generate(self, prompt: str, kind: str) -> str:
        response = self._model.generate_content(prompt)
        self._record_usage(kind, response)
        return response.text

    def _record_usage(self, kind: str, response) -> None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        prompt_tokens = usage.prompt_token_count
        completion_tokens = usage.candidates_token_count
        pricing = PRICING_PER_MILLION_TOKENS.get(self.model_name)
        cost_usd = None
        if pricing is not None:
            input_price, output_price = pricing
            cost_usd = (prompt_tokens / 1_000_000) * input_price + (completion_tokens / 1_000_000) * output_price
        elif self.model_name not in _unpriced_models_warned:
            _unpriced_models_warned.add(self.model_name)
            logger.warning("No verified pricing for model %s; llm_cost_usd_total will not include it", self.model_name)
        Metrics.mark_llm_usage(kind, prompt_tokens, completion_tokens, cost_usd)

    def summarize(self, email: EmailMessage) -> str:
        if not self._enabled:
            return "Gemini not configured."
        prompt = (
            "Provide a concise summary (max 4 bullet points) of the following email context:\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        return self._generate(prompt, kind="summary")

    def extract_job_entities(self, email: EmailMessage) -> dict:
        if not self._enabled:
            return {}

        fields = ", ".join(JOB_ENTITY_FIELDS)
        prompt = (
            f"Extract these fields from the job-related email below as strict JSON only "
            f"(no markdown fences, no commentary): {fields}. Use null when a field isn't mentioned.\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        try:
            raw = self._generate(prompt, kind="entities")
            return json.loads(raw)
        except (json.JSONDecodeError, AttributeError, ValueError) as exc:
            logger.warning("Failed to parse Gemini job entity extraction: %s", exc)
            return {}

    def draft_reply(self, email: EmailMessage) -> str:
        if not self._enabled:
            return "Gemini not configured."
        prompt = (
            "Draft a professional email response to the thread below. Keep it short and actionable.\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        return self._generate(prompt, kind="draft")
