import logging

import requests

from src.domain import EmailMessage, TriageResult
from src.observability.metrics import Metrics
from src.ports import EmailClassifier

logger = logging.getLogger(__name__)

# KeyError/ValueError/TypeError cover a malformed or non-JSON payload from the primary.
RECOVERABLE_ERRORS = (requests.RequestException, KeyError, ValueError, TypeError)


class FallbackClassifier:
    def __init__(self, primary: EmailClassifier, secondary: EmailClassifier) -> None:
        self.primary = primary
        self.secondary = secondary

    @property
    def is_configured(self) -> bool:
        return self.primary.is_configured or self.secondary.is_configured

    def check_connection(self) -> str:
        return self.primary.check_connection()

    def classify(self, email: EmailMessage) -> TriageResult:
        if self.primary.is_configured:
            try:
                return self.primary.classify(email)
            except RECOVERABLE_ERRORS as exc:
                logger.warning(
                    "%s failed (%s), falling back to %s",
                    type(self.primary).__name__,
                    exc,
                    type(self.secondary).__name__,
                )
                Metrics.mark_jev_fallback()
        return self.secondary.classify(email)
