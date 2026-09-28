import logging
from dataclasses import dataclass

import requests

from src.gmail.client import EmailMessage
from src.observability.metrics import Metrics

logger = logging.getLogger(__name__)


@dataclass
class TriageResult:
    urgency: str
    category: str
    confidence: float


URGENCIES = {
    "low": "Can wait; no action or reply is expected soon",
    "medium": "Needs attention within a few days",
    "high": "Needs attention today or has an imminent deadline",
}
CATEGORIES = {
    "offer": "Job offer, recruiter outreach or business opportunity",
    "urgent": "Time-critical matter requiring a prompt personal response",
    "spam": "Unsolicited or irrelevant bulk or scam email",
    "newsletter": "Subscribed newsletter, digest or automated notification",
    "general": "Any other email",
}
JEV_MODEL = "jev-latest"


class DecisionEngineClient:
    def __init__(self, api_url: str, api_key: str = "", timeout: int = 10) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.api_key)

    @staticmethod
    def _build_request(email: EmailMessage) -> dict:
        return {
            "model": JEV_MODEL,
            "state": {
                "subject": email.subject,
                "body": email.body or email.snippet,
                "sender": email.sender,
            },
            "questions": {
                "urgency": {
                    "type": "choice",
                    "instructions": "How urgent is this email for its recipient?",
                    "criteria": URGENCIES,
                },
                "category": {
                    "type": "choice",
                    "instructions": "Which category best describes this email?",
                    "criteria": CATEGORIES,
                },
            },
        }

    @classmethod
    def _parse_answers(cls, data: dict) -> TriageResult:
        answers = data["answers"]
        urgency, category = answers["urgency"], answers["category"]
        return TriageResult(
            urgency=cls._normalize_urgency(urgency.get("choice")),
            category=cls._normalize_category(category.get("choice")),
            confidence=min(float(urgency.get("confidence", 0.0)), float(category.get("confidence", 0.0))),
        )

    def classify(self, email: EmailMessage) -> TriageResult:
        if self.enabled:
            try:
                response = requests.post(
                    self.api_url,
                    json=self._build_request(email),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return self._parse_answers(response.json())
            except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
                logger.warning("JEV API failed at %s (%s), falling back to heuristic", self.api_url, exc)
                Metrics.mark_jev_fallback()

        return self._fallback_classification(email)

    @staticmethod
    def _fallback_classification(email: EmailMessage) -> TriageResult:
        text = f"{email.subject} {email.snippet}".lower()
        sender = f"{email.sender} {email.sender_domain}".lower()

        if any(term in sender for term in ["no-reply", "noreply", "newsletter"]) or "unsubscribe" in text:
            return TriageResult(urgency="low", category="newsletter", confidence=0.70)

        if any(term in text for term in ["urgent", "asap", "immediately", "deadline"]):
            return TriageResult(urgency="high", category="urgent", confidence=0.76)

        if any(term in text for term in ["offer", "interview", "recruiter", "position"]):
            return TriageResult(urgency="medium", category="offer", confidence=0.68)

        return TriageResult(urgency="low", category="general", confidence=0.60)

    @staticmethod
    def _normalize_urgency(value: str) -> str:
        value = (value or "").lower()
        return value if value in {"low", "medium", "high"} else "low"

    @staticmethod
    def _normalize_category(value: str) -> str:
        value = (value or "").lower()
        return value if value in {"offer", "general", "urgent", "spam", "newsletter"} else "general"
