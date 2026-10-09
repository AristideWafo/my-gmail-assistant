import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Protocol

from src.domain import AgentRun, Trajectory
from src.observability.metrics import Metrics
from src.ports import AgentRuns

logger = logging.getLogger(__name__)

INTERRUPTED = "interrupted"
OVER_BUDGET = "over_budget"
CRASHED = "crashed"
UNKNOWN_KIND = "unknown_kind"
IDLE_SECONDS = 5.0


class RunHandler(Protocol):
    def run(self, run: AgentRun) -> Trajectory:
        """Carries the run out, including telling whoever asked what came of it."""
        ...

    def gave_up(self, run: AgentRun, reason: str) -> None:
        """The run will not happen or did not finish: whoever asked must not wait in silence."""
        ...


class AgentBudget:
    """What the agent may still spend today. Kept apart from the analyzer's budget: a day of
    questions in the chat must not cost an urgent mail its summary."""

    def __init__(
        self,
        runs: AgentRuns,
        daily_usd: float,
        daily_runs: int,
        timezone: tzinfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runs = runs
        self._daily_usd = daily_usd
        self._daily_runs = daily_runs
        self._timezone = timezone
        self._clock = clock

    def exhausted(self) -> bool:
        local = self._clock().astimezone(self._timezone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        # Counted in runs too: a model without a known price costs nothing the store can add up.
        if self._runs.started_since(midnight) >= self._daily_runs:
            return True
        return self._daily_usd > 0 and self._runs.cost_since(midnight) >= self._daily_usd


class AgentWorker:
    """Carries queued runs out one at a time, on its own thread: neither the mail polling nor
    the chat buttons wait for a model."""

    def __init__(
        self, runs: AgentRuns, handlers: Mapping[str, RunHandler], budget: AgentBudget
    ) -> None:
        self._runs = runs
        self._handlers = handlers
        self._budget = budget
        self._wake = threading.Event()

    def notify(self) -> None:
        self._wake.set()

    def recover(self) -> None:
        """A run cut short by a stop is not taken up again: its tools may have run halfway and
        its question may be stale. Whoever asked is told instead."""
        for run in self._runs.fail_interrupted(INTERRUPTED):
            Metrics.mark_agent_run(run.kind, INTERRUPTED)
            self._give_up(run, INTERRUPTED)

    def run_forever(self, stopping: threading.Event) -> None:
        self.recover()
        while not stopping.is_set():
            if not self.run_one():
                self._wake.wait(IDLE_SECONDS)
                self._wake.clear()

    def run_one(self) -> bool:
        """Returns False when nothing was queued."""
        exhausted = self._budget.exhausted()
        run = self._runs.take_next()
        Metrics.agent_queue.set(self._runs.queued())
        if run is None:
            return False
        handler = self._handlers.get(run.kind)
        if handler is None or exhausted:
            self._refuse(run, UNKNOWN_KIND if handler is None else OVER_BUDGET)
            return True
        try:
            trajectory = handler.run(run)
        except Exception:
            logger.exception("Agent run %s (%s) crashed", run.id, run.kind)
            self._refuse(run, CRASHED)
            return True
        self._runs.finish(run.id, trajectory)
        Metrics.mark_agent_run(run.kind, trajectory.outcome)
        return True

    def _refuse(self, run: AgentRun, reason: str) -> None:
        self._runs.fail(run.id, reason)
        Metrics.mark_agent_run(run.kind, reason)
        self._give_up(run, reason)

    def _give_up(self, run: AgentRun, reason: str) -> None:
        handler = self._handlers.get(run.kind)
        if handler is None:
            logger.warning("Agent run %s has an unknown kind %r", run.id, run.kind)
            return
        try:
            handler.gave_up(run, reason)
        except Exception:
            logger.exception("Could not tell that agent run %s gave up (%s)", run.id, reason)
