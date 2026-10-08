from collections.abc import Callable
from datetime import timedelta
from typing import Protocol, runtime_checkable

from src.domain import (
    Correction,
    DecisionRecord,
    EmailMessage,
    FeedbackTally,
    RatedDecision,
    RuleCandidate,
    TrackedThread,
    TriageResult,
)


@runtime_checkable
class ThreadStore(Protocol):
    def get(self, thread_id: str) -> TrackedThread | None: ...

    def save(self, thread: TrackedThread) -> None: ...

    def update(
        self, thread_id: str, change: Callable[[TrackedThread | None], TrackedThread | None]
    ) -> TrackedThread | None:
        """Applies `change` to the stored thread atomically; use it for any read-modify-write.

        `change` returning None leaves the row as it is."""
        ...

    def in_state(self, state: str) -> list[TrackedThread]:
        """Earliest due first."""
        ...

    def counts(self) -> dict[str, int]: ...

    def prune(self, older_than: timedelta) -> int:
        """Forgets settled threads only; one still awaiting an answer is never pruned."""
        ...


@runtime_checkable
class DecisionStore(Protocol):
    @property
    def threads(self) -> ThreadStore: ...

    def record_decision(
        self, email: EmailMessage, triage: TriageResult, route: str, put_forward: bool = False
    ) -> None: ...

    def get(self, message_id: str) -> DecisionRecord | None: ...

    def attach_chat_message(self, message_id: str, chat_message_id: int) -> None: ...

    def find_by_chat_message(self, chat_message_id: int) -> DecisionRecord | None: ...

    def record_feedback(self, message_id: str, verdict: str, origin: str = "alert") -> bool: ...

    def recent_corrections(
        self, limit: int, verdicts: tuple[str, ...] | None = None
    ) -> list[Correction]:
        """Newest first; `verdicts` restricts them, None means every verdict but "valid"."""
        ...

    def review_candidates(self, max_age: timedelta, limit: int) -> list[DecisionRecord]:
        """Recent unrated decisions the classifier made without alerting, newest first."""
        ...

    def decisions_since(self, max_age: timedelta) -> list[DecisionRecord]:
        """Every decision of the period, oldest first."""
        ...

    def put_forward_pending(self, max_age: timedelta) -> list[DecisionRecord]:
        """Recent mails put forward and not rated yet, oldest first."""
        ...

    def feedback_counts(self) -> dict[str, int]: ...

    def feedback_breakdown(self) -> list[FeedbackTally]: ...

    def decision_counts_by_source(self, max_age: timedelta) -> dict[str, int]: ...

    def mark_alerted(self, message_id: str) -> None: ...

    def was_alerted(self, message_id: str, within_seconds: float | None = None) -> bool: ...

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...

    def rated_decisions(self) -> list[RatedDecision]:
        """Every decision the user gave a verdict on, oldest verdict first."""
        ...

    def rule_candidates(self, min_count: int, max_age: timedelta) -> list[RuleCandidate]:
        """Senders the classifier always judged the same way and the user never corrected."""
        ...

    def archived_streak(self, sender: str, max_age: timedelta) -> int:
        """Mails from `sender` archived over the period, or 0 if any was kept or wanted back."""
        ...

    def prune(self, older_than: timedelta) -> int: ...

    def backup(self, destination: str) -> None:
        """Writes a consistent, verified copy to `destination`; raises when it cannot."""
        ...

    def close(self) -> None: ...
