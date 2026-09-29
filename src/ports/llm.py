from typing import Protocol, runtime_checkable

from src.domain import EmailMessage, LLMAnalysis


@runtime_checkable
class EmailAnalyzer(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def check_connection(self) -> str: ...

    def analyze(self, email: EmailMessage, want_draft: bool, want_entities: bool) -> LLMAnalysis: ...
