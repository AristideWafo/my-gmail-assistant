import logging
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from src.domain import EmailMessage, TriageResult
from src.triage.few_shot import FEW_SHOT_INSTRUCTION
from src.triage.taxonomy import CATEGORIES, URGENCIES

logger = logging.getLogger(__name__)


URGENCY_INSTRUCTIONS = (
    "How urgent is this email for its recipient? Judge deadlines against today's date and the received date. "
    "Bulk, automated or marketing mail is never high."
)
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
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self.examples_provider = examples_provider

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

    def _build_request(self, email: EmailMessage) -> dict:
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
            for question in request["questions"].values():
                question["instructions"] = f"{question['instructions']} {FEW_SHOT_INSTRUCTION}"
        return request

    @classmethod
    def _parse_answers(cls, data: dict) -> TriageResult:
        answers = data["answers"]
        urgency, category = answers["urgency"], answers["category"]
        return TriageResult(
            urgency=cls._normalize_urgency(urgency.get("choice")),
            category=cls._normalize_category(category.get("choice")),
            confidence=min(float(urgency.get("confidence", 0.0)), float(category.get("confidence", 0.0))),
            source="jev",
        )

    def classify(self, email: EmailMessage) -> TriageResult:
        response = requests.post(
            self.api_url,
            json=self._build_request(email),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._parse_answers(response.json())

    @staticmethod
    def _normalize_urgency(value: str) -> str:
        value = (value or "").lower()
        return value if value in {"low", "medium", "high"} else "low"

    @staticmethod
    def _normalize_category(value: str) -> str:
        value = (value or "").lower()
        return value if value in CATEGORIES else "personnel"
