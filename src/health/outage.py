import logging
import time
from collections.abc import Callable

from .connections import describe_failure

logger = logging.getLogger(__name__)


def format_outage(down_for_seconds: float, error: Exception) -> str:
    return (
        f"⚠️ Relève des mails en échec depuis {_minutes(down_for_seconds)} min : "
        f"{describe_failure(error)}. Aucun mail n'est trié tant que ça dure."
    )


def format_recovery(down_for_seconds: float) -> str:
    return f"✅ Relève des mails rétablie après {_minutes(down_for_seconds)} min d'interruption."


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


class OutageNotifier:
    """Tells the user when mail fetching has kept failing, then when it works again."""

    def __init__(
        self,
        alert_after_seconds: float,
        notify: Callable[[str], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._alert_after = alert_after_seconds
        self._notify = notify
        self._clock = clock
        self._failing_since: float | None = None
        self._announced = False

    @property
    def enabled(self) -> bool:
        return self._alert_after > 0

    def record_failure(self, error: Exception) -> None:
        if not self.enabled:
            return
        now = self._clock()
        if self._failing_since is None:
            self._failing_since = now
        down_for = now - self._failing_since
        if self._announced or down_for < self._alert_after:
            return
        # An undelivered message is retried on the next failing cycle: the chat is often down
        # for the same reason the mail is (no network).
        self._announced = self._send(format_outage(down_for, error))

    def record_success(self) -> None:
        failing_since, announced = self._failing_since, self._announced
        self._failing_since, self._announced = None, False
        if not self.enabled or failing_since is None:
            return
        down_for = self._clock() - failing_since
        # Also sent when the outage message never got through, so a long gap is never silent.
        if announced or down_for >= self._alert_after:
            self._send(format_recovery(down_for))

    def _send(self, text: str) -> bool:
        try:
            return bool(self._notify(text))
        except Exception:
            # Called from the polling loop, which must survive anything.
            logger.exception("Failed to send the polling outage message")
            return False
