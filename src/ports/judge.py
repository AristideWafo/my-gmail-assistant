from typing import Protocol, runtime_checkable


@runtime_checkable
class SentMailJudge(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def expects_answer(self, subject: str, text: str) -> float:
        """Probability that a mail I wrote waits for an answer; raises when it cannot tell."""
        ...
