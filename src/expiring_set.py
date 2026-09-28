import time
from collections.abc import Callable, Hashable


class ExpiringSet:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._expires_at: dict[Hashable, float] = {}

    def add(self, key: Hashable) -> None:
        self._expires_at[key] = self._clock() + self._ttl

    def __contains__(self, key: Hashable) -> bool:
        self._prune()
        return key in self._expires_at

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, expires_at in self._expires_at.items() if expires_at <= now]:
            del self._expires_at[key]
