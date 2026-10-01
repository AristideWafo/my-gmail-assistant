import logging
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from src.domain import EmailMessage, TriageResult
from src.observability.metrics import Metrics
from src.triage.attention import ATTENTION_QUESTIONS
from src.triage.few_shot import FEW_SHOT_INSTRUCTION
from src.triage.taxonomy import CATEGORIES, URGENCIES

logger = logging.getLogger(__name__)


URGENCY_INSTRUCTIONS = (
    "How urgent is this email for its recipient? Judge deadlines against today's date and the received date. "
    "Bulk, automated or marketing mail is never high."
)
# Measured on the live API: the bare question "does this email expect a reply?" says yes to
# newsletters and scams that ask to be answered, hence the explicit exclusions and criteria.
NEEDS_REPLY_QUESTION = {
    "type": "noul",
    "instructions": (
        "Does a person who wrote this email expect a written reply from its recipient? "
        "Judge from `body` first, then `subject`. Text asking to reply inside bulk, marketing, "
        "automated or scam mail does not count."
    ),
    "criteria": {
        "true": "A human sender asks a question, requests something, proposes something or waits "
        "for a confirmation that the recipient has to answer by email",
        "false": "Nothing to answer: information only, thanks, automated notification, newsletter, "
        "promotion, scam, or an action to do elsewhere than by replying",
    },
}
# The examples only carry urgency and category corrections.
FEW_SHOT_QUESTIONS = ("urgency", "category")
JEV_MODEL = "jev-latest"
# KeyError/ValueError/TypeError cover a malformed or non-JSON JEV payload.
JEV_RECOVERABLE_ERRORS = (requests.RequestException, KeyError, ValueError, TypeError)


class JevClassifier:
    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        timeout: int = 10,
        examples_provider: Callable[[], list[dict[str, str]]] | None = None,
        ask_needs_reply: bool = False,
        ask_attention: bool = False,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self.examples_provider = examples_provider
        self.ask_needs_reply = ask_needs_reply
        self.ask_attention = ask_attention

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def check_connection(self) -> str:
        request = {
            "model": JEV_MODEL,
            "state": "ping",
            "questions": {
                "ping": {"type": "noul", "instructions": "Is this a test?", "criteria": {"true": "yes", "false": "no"}}
            },
        }
        response = requests.post(
            self.api_url, json=request, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout
        )
        response.raise_for_status()
        return "API key accepted"

    def _load_examples(self) -> list[dict[str, str]]:
        if self.examples_provider is None:
            return []
        try:
            return self.examples_provider()
        except Exception as exc:  # noqa: BLE001 - learning is best-effort, never block triage
            logger.warning("Few-shot examples unavailable (%s), classifying without them", exc)
            return []

    def build_request(self, email: EmailMessage) -> dict:
        request = {
            "model": JEV_MODEL,
            "state": {
                "subject": email.subject,
                "body": email.body or email.snippet,
                "sender": email.sender,
                "received_at": email.received_at,
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
        examples = self._load_examples()
        if examples:
            request["state"]["examples"] = examples
            for name in FEW_SHOT_QUESTIONS:
                question = request["questions"][name]
                question["instructions"] = f"{question['instructions']} {FEW_SHOT_INSTRUCTION}"
        if self.ask_needs_reply:
            request["questions"]["needs_reply"] = NEEDS_REPLY_QUESTION
        if self.ask_attention:
            request["questions"].update(ATTENTION_QUESTIONS)
        return request

    @classmethod
    def parse_answers(cls, data: dict) -> TriageResult:
        answers = data["answers"]
        urgency, category = answers["urgency"], answers["category"]
        return TriageResult(
            urgency=cls._normalize_urgency(urgency.get("choice")),
            category=cls._normalize_category(category.get("choice")),
            confidence=min(float(urgency.get("confidence", 0.0)), float(category.get("confidence", 0.0))),
            source="jev",
            needs_reply=cls.parse_probability(answers.get("needs_reply")),
            signals={
                name: probability
                for name in ATTENTION_QUESTIONS
                if (probability := cls.parse_probability(answers.get(name))) is not None
            },
        )

    @staticmethod
    def parse_probability(answer: object) -> float | None:
        # Optional on purpose: a missing or odd answer must not fail the whole classification
        # and send the mail to the heuristic fallback.
        probability = answer.get("noul") if isinstance(answer, dict) else None
        if isinstance(probability, bool) or not isinstance(probability, int | float):
            return None
        return min(max(float(probability), 0.0), 1.0)

    @staticmethod
    def _record_usage(data: dict) -> None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        try:
            tokens = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        except (TypeError, ValueError):
            return
        Metrics.mark_llm_usage("triage", *tokens, cost_usd=None)

    def ask(self, request: dict) -> dict:
        response = requests.post(
            self.api_url,
            json=request,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def classify(self, email: EmailMessage) -> TriageResult:
        data = self.ask(self.build_request(email))
        result = self.parse_answers(data)
        self._record_usage(data)
        return result

    @staticmethod
    def _normalize_urgency(value: str) -> str:
        value = (value or "").lower()
        return value if value in {"low", "medium", "high"} else "low"

    @staticmethod
    def _normalize_category(value: str) -> str:
        value = (value or "").lower()
        return value if value in CATEGORIES else "personnel"
