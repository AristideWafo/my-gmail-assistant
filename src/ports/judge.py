from typing import Protocol, runtime_checkable


@runtime_checkable
class SentMailJudge(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def expects_answer(self, subject: str, text: str) -> float:
        """Probability that a mail I wrote waits for an answer; raises when it cannot tell."""
        ...


@runtime_checkable
class QuestionJudge(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def probability(self, state: dict[str, str], question: str, yes: str, no: str) -> float:
        """Probability that the answer to a yes/no question about `state` is yes, `yes` and
        `no` saying what each means; raises when it cannot tell."""
        ...
