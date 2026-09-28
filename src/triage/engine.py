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


class DecisionEngineClient:
    def __init__(self, api_url: str, timeout: int = 10) -> None:
        self.api_url = api_url
        self.timeout = timeout

    def classify(self, email: EmailMessage) -> TriageResult:
        payload = {
            "subject": email.subject,
            "body": email.body or email.snippet,
            "sender": email.sender,
        }

        if self.api_url:
            try:
                response = requests.post(self.api_url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                return TriageResult(
                    urgency=self._normalize_urgency(data.get("urgency", "low")),
                    category=self._normalize_category(data.get("category", "general")),
                    confidence=float(data.get("confidence", 0.0)),
                )
            except requests.RequestException as exc:
                logger.warning("JEV API unreachable at %s (%s), falling back to heuristic", self.api_url, exc)
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
