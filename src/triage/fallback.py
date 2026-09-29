import logging
from collections.abc import Callable

from src.domain import EmailMessage, TriageResult
from src.ports import EmailClassifier

logger = logging.getLogger(__name__)

class FallbackClassifier:
    def __init__(
        self,
        primary: EmailClassifier,
        secondary: EmailClassifier,
        recoverable: tuple[type[Exception], ...],
        on_fallback: Callable[[], None] = lambda: None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self._recoverable = recoverable
        self._on_fallback = on_fallback

    @property
    def is_configured(self) -> bool:
        return self.primary.is_configured or self.secondary.is_configured

    def check_connection(self) -> str:
        return self.primary.check_connection()

    def classify(self, email: EmailMessage) -> TriageResult:
        if self.primary.is_configured:
            try:
                return self.primary.classify(email)
            except self._recoverable as exc:
                logger.warning(
                    "%s failed (%s), falling back to %s",
                    type(self.primary).__name__,
                    exc,
                    type(self.secondary).__name__,
                )
                self._on_fallback()
        return self.secondary.classify(email)
