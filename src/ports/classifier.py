from typing import Protocol, runtime_checkable

from src.domain import EmailMessage, TriageResult


@runtime_checkable
class EmailClassifier(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def check_connection(self) -> str: ...

    def classify(self, email: EmailMessage) -> TriageResult: ...
