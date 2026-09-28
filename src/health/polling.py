import asyncio
import time
from collections.abc import Callable


class PollHealth:
    """Heartbeat of the polling loop: a dead task or a stalled loop means alerts silently stop."""

    def __init__(self, stale_after_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._stale_after = stale_after_seconds
        self._clock = clock
        self._last_beat = clock()

    def beat(self) -> None:
        self._last_beat = self._clock()

    def problem(self, task_done: bool) -> str | None:
        if task_done:
            return "polling task has stopped"
        idle = self._clock() - self._last_beat
        if idle > self._stale_after:
            return f"no polling activity for {idle:.0f}s (limit {self._stale_after:.0f}s)"
        return None


async def run_watchdog(
    health: PollHealth,
    task_done: Callable[[], bool],
    on_failure: Callable[[str], None],
    interval_seconds: float = 30.0,
) -> None:
    """Escalates to `on_failure` (which should terminate the process so the restart policy recovers it)."""
    while True:
        await asyncio.sleep(interval_seconds)
        reason = health.problem(task_done())
        if reason:
            on_failure(reason)
            return
