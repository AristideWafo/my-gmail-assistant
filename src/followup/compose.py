from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.domain import RUN_QUEUED, RUN_RUNNING, TrackedThread
from src.ports import AgentRuns

KIND = "followup"
# Past this an offer no longer waits for the agent's text and goes out with the fixed one: a
# worker that is down or busy must not hold follow-ups back.
MAX_WAIT = timedelta(minutes=30)


def trigger_key(thread: TrackedThread) -> str:
    """One run per anchor: a thread is written for once, whatever the number of refreshes."""
    return f"{KIND}:{thread.thread_id}:{thread.anchor.message_id}"


def is_composed(thread: TrackedThread) -> bool:
    return bool(thread.composed_text) and thread.composed_for == thread.anchor.message_id


class ComposedFollowUps:
    """Asks the agent to write the follow-ups that became due, and tells the offers whether
    its text is there. `use_in_offers` off is the observation mode: written, stored, not shown."""

    def __init__(
        self,
        runs: AgentRuns,
        notify: Callable[[], None],
        use_in_offers: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runs = runs
        self._notify = notify
        self._use_in_offers = use_in_offers
        self._clock = clock

    def request(self, thread: TrackedThread) -> None:
        if is_composed(thread):
            return
        payload = {"thread_id": thread.thread_id, "anchor_id": thread.anchor.message_id}
        if self._runs.enqueue(trigger_key(thread), KIND, payload):
            self._notify()

    def still_writing(self, thread: TrackedThread) -> bool:
        """True while an offer should wait for the agent's text."""
        if not self._use_in_offers or is_composed(thread):
            return False
        run = self._runs.get(trigger_key(thread))
        if run is None:
            self.request(thread)
            return True
        waiting = run.state in (RUN_QUEUED, RUN_RUNNING)
        return waiting and self._clock() - run.created_at < MAX_WAIT

    def text_for(self, thread: TrackedThread) -> str | None:
        return thread.composed_text if self._use_in_offers and is_composed(thread) else None

    def advice_for(self, thread: TrackedThread) -> str:
        return thread.composed_advice if self._use_in_offers and is_composed(thread) else ""
