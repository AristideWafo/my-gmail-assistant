import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from src.formatting import clean_draft, has_placeholder, strip_markdown
from src.gmail.client import EmailMessage
from src.llm.rate_limit import RateLimiter
from src.observability.metrics import Metrics

logger = logging.getLogger(__name__)

JOB_ENTITY_FIELDS = ("poste", "entreprise", "stack", "salaire", "prochaine_etape")
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 60.0
DEFAULT_RETRY_DELAY_SECONDS = 20.0
_RETRY_DELAY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)

# $/1M tokens (input, output), verified on ai.google.dev/gemini-api/docs/pricing.
# Older models (1.5/2.0 flash) are retired and no longer listed there; add only prices you can verify.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
}
_unpriced_models_warned: set[str] = set()


@dataclass
class LLMAnalysis:
    summary: str = ""
    draft: str = ""
    entities: dict = field(default_factory=dict)


def _retry_delay(exc: Exception) -> float:
    match = _RETRY_DELAY_RE.search(str(exc))
    return float(match.group(1)) if match else DEFAULT_RETRY_DELAY_SECONDS


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-1.5-flash",
        max_rpm: int = 12,
        user_name: str = "",
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_name = model_name
        self._user_name = user_name
        self._limiter = limiter or RateLimiter(max_rpm)
        self._sleep = sleep
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

    def _generate(self, prompt: str, kind: str, json_output: bool = False) -> str:
        options = {"generation_config": {"response_mime_type": "application/json"}} if json_output else {}
        for attempt in range(MAX_RETRIES + 1):
            self._limiter.acquire()
            try:
                response = self._model.generate_content(prompt, **options)
            except ResourceExhausted as exc:
                Metrics.mark_llm_error("rate_limited")
                if attempt == MAX_RETRIES:
                    raise
                delay = min(_retry_delay(exc), MAX_RETRY_DELAY_SECONDS)
                logger.warning("Gemini quota exceeded; retrying in %.0fs (attempt %d)", delay, attempt + 1)
                self._sleep(delay)
                continue
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

    def analyze(self, email: EmailMessage, want_draft: bool, want_entities: bool) -> LLMAnalysis:
        """One call yields everything needed, so each email costs a single request against the RPM quota."""
        if not self._enabled:
            return LLMAnalysis()
        raw = self._generate(self._build_prompt(email, want_draft, want_entities), kind="analysis", json_output=True)
        return self._parse_analysis(raw, want_draft, want_entities)

    def _build_prompt(self, email: EmailMessage, want_draft: bool, want_entities: bool) -> str:
        keys = ["summary"] + (["draft"] if want_draft else []) + (["entities"] if want_entities else [])
        rules = [
            '- "summary": résumé en français en 2 à 4 puces, chaque ligne commence par "- ", texte brut sans markdown.'
        ]
        if want_draft:
            signature = f'puis la signature "{self._user_name}"' if self._user_name else "sans signature nominative"
            rules.append(
                '- "draft": corps de la réponse à envoyer, dans la langue de l\'e-mail reçu. Uniquement le texte du '
                'message: aucune introduction du type "Voici une proposition", aucun objet, aucun commentaire, '
                "aucun markdown, aucun texte à compléter entre crochets. Commence par la salutation, termine par "
                f"une formule de politesse courte {signature}. Court et concret."
            )
        if want_entities:
            fields = ", ".join(JOB_ENTITY_FIELDS)
            rules.append(f'- "entities": objet avec les champs {fields} (null si absent).')
        return (
            f"Tu analyses un e-mail reçu. Réponds uniquement par un objet JSON avec les clés: {', '.join(keys)}.\n"
            + "\n".join(rules)
            + f"\n\nObjet: {email.subject}\nExpéditeur: {email.sender}\nCorps: {email.body or email.snippet}"
        )

    @staticmethod
    def _parse_analysis(raw: str, want_draft: bool, want_entities: bool) -> LLMAnalysis:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("expected a JSON object")
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse Gemini analysis as JSON: %s", exc)
            Metrics.mark_llm_error("parse")
            return LLMAnalysis()

        draft = clean_draft(str(data.get("draft") or "")) if want_draft else ""
        if has_placeholder(draft):
            # A draft with "[Your Name]" left in is worse than none: it could be sent as is.
            Metrics.mark_llm_error("placeholder")
            draft = ""
        entities = data.get("entities")
        return LLMAnalysis(
            summary=strip_markdown(str(data.get("summary") or "")),
            draft=draft,
            entities=entities if want_entities and isinstance(entities, dict) else {},
        )
