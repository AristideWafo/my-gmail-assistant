from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from src.config import in_quiet_hours
from src.ports import DecisionStore

BUDGET_KEY_PREFIX = "proactive:"


class ProactiveBudget:
    """How many unsolicited messages may still go out today; none during quiet hours."""

    def __init__(
        self,
        store: DecisionStore,
        timezone: tzinfo,
        daily_cap: int,
        quiet_hours: tuple[int, int] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._timezone = timezone
        self._daily_cap = daily_cap
        self._quiet_hours = quiet_hours
        self._clock = clock

    def available(self) -> int:
        local = self._clock().astimezone(self._timezone)
        if in_quiet_hours(local.hour, self._quiet_hours):
            return 0
        return max(self._daily_cap - self._used(local), 0)

    def spend(self, units: int = 1) -> None:
        """Called once the message went out: a failed send must not use up the day's budget."""
        local = self._clock().astimezone(self._timezone)
        self._store.set_state(self._key(local), str(self._used(local) + units))

    def _used(self, local: datetime) -> int:
        return int(self._store.get_state(self._key(local)) or 0)

    @staticmethod
    def _key(local: datetime) -> str:
        return f"{BUDGET_KEY_PREFIX}{local.date().isoformat()}"
