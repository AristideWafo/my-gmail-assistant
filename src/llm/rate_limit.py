import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limiter: blocks until a call fits under `max_calls` per `window_seconds`."""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._calls and self._calls[0] <= now - self._window:
                self._calls.popleft()
            if len(self._calls) < self._max_calls:
                self._calls.append(now)
                return
            self._sleep(self._calls[0] + self._window - now)
