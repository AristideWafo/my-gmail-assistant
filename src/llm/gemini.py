import json
import logging
import re
import time
from collections.abc import Callable

import google.generativeai as genai
from google.api_core.exceptions import DeadlineExceeded, ResourceExhausted

from src.agent.rules import NO_COMMITMENT_RULE
from src.domain import EmailMessage, LLMAnalysis
from src.formatting import clean_draft, has_placeholder, strip_markdown
from src.llm.pricing import estimate_cost_usd
from src.llm.rate_limit import RateLimiter
from src.observability.metrics import Metrics

logger = logging.getLogger(__name__)

JOB_ENTITY_FIELDS = ("poste", "entreprise", "stack", "salaire", "prochaine_etape")
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 60.0
DEFAULT_RETRY_DELAY_SECONDS = 20.0
_RETRY_DELAY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)


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
        timeout_seconds: float = 30.0,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_name = model_name
        self._user_name = user_name
        # Without a deadline a stalled call blocks the polling thread until the watchdog restarts
        # the container, and every mail behind it waits.
        self._request_options = {"timeout": timeout_seconds}
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
        genai.get_model(f"models/{self.model_name}", request_options=self._request_options)
        return f"model {self.model_name} available"

    def _generate(self, prompt: str, kind: str, json_output: bool = False) -> str:
        options = {"generation_config": {"response_mime_type": "application/json"}} if json_output else {}
        for attempt in range(MAX_RETRIES + 1):
            self._limiter.acquire()
            try:
                response = self._model.generate_content(
                    prompt, request_options=self._request_options, **options
                )
            except DeadlineExceeded:
                # Not retried: the caller has a degraded path, and waiting again would hold the
                # polling thread for another full deadline.
                Metrics.mark_llm_error("timeout")
                raise
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
        cost_usd = estimate_cost_usd(self.model_name, prompt_tokens, completion_tokens)
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
                f"une formule de politesse courte {signature}. Court et concret. "
                f"{NO_COMMITMENT_RULE}"
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
