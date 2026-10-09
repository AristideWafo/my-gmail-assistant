from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from src.domain import (
    AgentRun,
    Correction,
    DecisionRecord,
    EmailMessage,
    FeedbackTally,
    PendingAction,
    RatedDecision,
    RuleCandidate,
    TrackedThread,
    Trajectory,
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

    def rated(self) -> list[TrackedThread]:
        """Threads given a follow-up verdict, whatever their state."""
        ...

    def counts(self) -> dict[str, int]: ...

    def prune(self, older_than: timedelta) -> int:
        """Forgets settled threads only; one still awaiting an answer is never pruned."""
        ...


@runtime_checkable
class PendingActions(Protocol):
    def propose(self, kind: str, payload: dict, lifetime: timedelta) -> PendingAction:
        """Stores the whole of what will be shown for confirmation; nothing is carried out."""
        ...

    def get(self, action_id: str) -> PendingAction | None: ...

    def pending_on(self, chat_message_id: int) -> PendingAction | None:
        """The action still awaiting a decision on that chat message, if any."""
        ...

    def attach_chat_message(self, action_id: str, chat_message_id: int) -> bool:
        """Binds a pending action to the message showing it, once: False when it already is."""
        ...

    def begin(self, action_id: str, chat_message_id: int) -> PendingAction | None:
        """Hands the action over to be carried out, once: None when it is not pending, has
        expired, was not offered on that chat message or is no longer what was stored."""
        ...

    def cancel(self, action_id: str, chat_message_id: int) -> bool: ...

    def finish(self, action_id: str, succeeded: bool) -> None: ...

    def prune(self, older_than: timedelta) -> int: ...


@runtime_checkable
class AgentRuns(Protocol):
    def enqueue(self, trigger_key: str, kind: str, payload: dict) -> bool:
        """Queues a run unless its trigger already queued one: False for the duplicate."""
        ...

    def get(self, trigger_key: str) -> AgentRun | None: ...

    def answered_before(self, run: AgentRun, since: datetime, limit: int) -> list[AgentRun]:
        """The last runs of the same kind that ended on an answer before this one, oldest
        first."""
        ...

    def take_next(self) -> AgentRun | None:
        """Hands the oldest queued run over, now running; None when the queue is empty."""
        ...

    def finish(self, run_id: int, trajectory: Trajectory) -> None: ...

    def fail(self, run_id: int, reason: str) -> None: ...

    def fail_interrupted(self, reason: str) -> list[AgentRun]:
        """Fails every run still marked running, which only a stop mid-run leaves behind."""
        ...

    def started_since(self, moment: datetime) -> int: ...

    def cost_since(self, moment: datetime) -> float:
        """Estimated cost of the runs started since then; a run of unknown price adds nothing."""
        ...

    def queued(self) -> int: ...

    def prune(self, older_than: timedelta) -> int:
        """Forgets finished runs only."""
        ...


@runtime_checkable
class DecisionStore(Protocol):
    @property
    def threads(self) -> ThreadStore: ...

    @property
    def pending_actions(self) -> PendingActions: ...

    @property
    def agent_runs(self) -> AgentRuns: ...

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

    def claim(self, key: str, value: str = "1") -> bool:
        """Sets the key only if it is unset, in one step: True for the single caller that may
        go on with what the key guards."""
        ...

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
