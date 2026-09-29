import logging
import threading
import time
from collections.abc import Callable

from src.domain import ChatEvent
from src.ports import ChatInbox

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


def run_listener(
    inbox: ChatInbox,
    dispatch: Callable[[ChatEvent], None],
    load_offset: Callable[[], int | None],
    save_offset: Callable[[int], None],
    stopping: threading.Event,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Delivers events at-least-once: the offset is saved only after the batch is dispatched."""
    offset = _safe_load_offset(load_offset)
    backoff = INITIAL_BACKOFF_SECONDS
    while not stopping.is_set():
        try:
            events, next_offset = inbox.get_updates(offset)
        except Exception as exc:  # noqa: BLE001 - the listener thread must never die
            # Only the type: adapter errors may embed credential-bearing URLs; adapters log their
            # own sanitized detail.
            logger.warning(
                "Chat polling failed (%s); retrying in %.0fs", type(exc).__name__, backoff
            )
            sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue
        backoff = INITIAL_BACKOFF_SECONDS
        for event in events:
            _safe_dispatch(dispatch, event)
        if next_offset is not None and next_offset != offset:
            offset = next_offset
            _safe_save_offset(save_offset, offset)


def _safe_load_offset(load_offset: Callable[[], int | None]) -> int | None:
    try:
        return load_offset()
    except Exception:
        logger.exception("Failed to load chat update offset; starting without one")
        return None


def _safe_dispatch(dispatch: Callable[[ChatEvent], None], event: ChatEvent) -> None:
    try:
        dispatch(event)
    except Exception:
        logger.exception("Chat event handler failed for %s", type(event).__name__)


def _safe_save_offset(save_offset: Callable[[int], None], offset: int) -> None:
    try:
        save_offset(offset)
    except Exception:
        logger.exception("Failed to persist chat update offset %s", offset)
