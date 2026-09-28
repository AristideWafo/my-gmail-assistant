import time
from collections.abc import Callable


class CircuitBreaker:
    """Stops calling a failing channel for `cooldown_seconds` after `failure_threshold` consecutive failures."""

    def __init__(
        self, failure_threshold: int = 5, cooldown_seconds: float = 600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return self._clock() >= self._open_until

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> bool:
        """Returns True when this failure just opened the circuit."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._failure_threshold:
            return False
        self._consecutive_failures = 0
        self._open_until = self._clock() + self._cooldown
        return True
