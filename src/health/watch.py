import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    name: str
    # What is wrong, in words for the user, or None when all is well.
    problem: Callable[[], str | None]
    # Sent when the problem is over; None for a problem that ends by itself, like a daily budget.
    recovery: str | None = None


class RecentIncrease:
    """How much a counter grew over the last `window_seconds`, sampled each time it is asked."""

    def __init__(
        self,
        read: Callable[[], float],
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._window = window_seconds
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque()

    def value(self) -> float:
        now, current = self._clock(), self._read()
        self._samples.append((now, current))
        # The newest sample at or before the window's start is the baseline, so the increase
        # covers the whole window rather than only what was sampled inside it.
        while len(self._samples) > 1 and self._samples[1][0] <= now - self._window:
            self._samples.popleft()
        return current - self._samples[0][1]


class HealthWatch:
    """Tells the user when a check starts failing, and when it stops."""

    def __init__(self, checks: Iterable[Check], notify: Callable[[str], bool]) -> None:
        self._checks = list(checks)
        self._notify = notify
        self._announced: set[str] = set()

    def run(self) -> None:
        for check in self._checks:
            try:
                problem = check.problem()
            except Exception:
                # Called from the polling loop, which must survive anything.
                logger.exception("Health check %s failed to run", check.name)
                continue
            if problem and check.name not in self._announced:
                # An undelivered message is tried again at the next cycle.
                if self._send(problem):
                    self._announced.add(check.name)
            elif not problem and check.name in self._announced:
                self._announced.discard(check.name)
                if check.recovery:
                    self._send(check.recovery)

    def _send(self, text: str) -> bool:
        try:
            return bool(self._notify(text))
        except Exception:
            logger.exception("Failed to send a health message")
            return False
