from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from src.ports import DecisionStore


class DailyJob:
    """Something to do once per local day, from a given hour on; restarts neither repeat nor skip it."""

    def __init__(
        self,
        name: str,
        hour: int,
        timezone: tzinfo,
        store: DecisionStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._key = f"job:{name}"
        self._hour = hour
        self._timezone = timezone
        self._store = store
        self._clock = clock

    def claim(self) -> bool:
        """True once per local day, the first time it is asked at or after the hour."""
        local = self._clock().astimezone(self._timezone)
        if local.hour < self._hour:
            return False
        today = local.date().isoformat()
        if self._store.get_state(self._key) == today:
            return False
        # Claimed before the work is done: a crash in the middle costs one day's run instead of
        # repeating it at every restart.
        self._store.set_state(self._key, today)
        return True
