import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

from src.domain import WAITING_FOR_THEM, TrackedThread
from src.followup.state import (
    DELETED,
    EXPIRED,
    add_business_days,
    close,
    derive,
    track,
    with_answer,
    would_propose,
)
from src.observability.metrics import Metrics
from src.ports import DecisionStore, MailProvider, SentMailJudge

logger = logging.getLogger(__name__)

ACTIVATED_AT_KEY = "followup:activated_at"
SENT_WINDOW_DAYS = 14
# A mail unanswered for a month is no longer a follow-up: its thread stops being re-read.
MAX_TRACKING = timedelta(days=30)
# Bounds the JEV calls of one refresh; the rest are asked at the next one.
MAX_JUDGED_PER_REFRESH = 20


class FollowUpTracker:
    """Keeps the threads holding a mail I sent in step with Gmail, from their headers only."""

    def __init__(
        self,
        mail: MailProvider,
        store: DecisionStore,
        timezone: tzinfo,
        after_days: int,
        max_threads: int,
        judge: SentMailJudge | None = None,
        threshold: float = 0.5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_due: Callable[[TrackedThread], None] | None = None,
    ) -> None:
        # Told of each thread a follow-up is due for, at every refresh until it is offered.
        self._on_due = on_due
        self._mail = mail
        self._store = store
        self._timezone = timezone
        self._after_days = after_days
        self._max_threads = max_threads
        self._judge = judge
        self.threshold = threshold
        self._clock = clock

    def activated_at(self) -> datetime:
        """When tracking started: only mail sent since then is followed, so turning it on does
        not offer to follow up two weeks of past mail at once."""
        raw = self._store.get_state(ACTIVATED_AT_KEY)
        if raw is not None:
            try:
                stored = datetime.fromisoformat(raw)
            except ValueError:
                stored = None
            if stored is not None and stored.tzinfo is not None:
                return stored
            # Starting again from now can only follow less mail, never more.
            logger.warning("Unreadable follow-up activation date %r; starting from now", raw)
        now = self._clock()
        self._store.set_state(ACTIVATED_AT_KEY, now.isoformat())
        return now

    def refresh(self) -> None:
        activated_at = self.activated_at()
        try:
            refs = self._mail.sent_threads(SENT_WINDOW_DAYS, self._max_threads + 1)
            mine = self._mail.my_addresses()
        except Exception as exc:  # noqa: BLE001 - the next refresh retries
            Metrics.mark_followup_refresh_error("list")
            logger.warning("Could not list sent threads: %s", exc)
            return
        Metrics.set_followup_capped(len(refs) > self._max_threads)
        refs = refs[: self._max_threads]

        threads = self._store.threads
        listed = {ref.thread_id for ref in refs}
        to_read = [
            ref.thread_id
            for ref in refs
            if (known := threads.get(ref.thread_id)) is None or known.history_id != ref.history_id
        ]
        # A thread outside the window is not listed any more, yet may still await an answer:
        # without a history id to compare, it is read at every refresh until it expires.
        unlisted = 0
        for waiting in threads.in_state(WAITING_FOR_THEM):
            if waiting.thread_id in listed:
                continue
            if self._expired(waiting):
                now = self._clock()
                threads.update(waiting.thread_id, lambda current, now=now: close(current, EXPIRED, now))
            else:
                to_read.append(waiting.thread_id)
                unlisted += 1
        Metrics.followup_unlisted_waiting.set(unlisted)

        for thread_id in to_read:
            try:
                self._read(thread_id, mine, activated_at)
            except Exception as exc:  # noqa: BLE001 - one unreadable thread must not stop the rest
                Metrics.mark_followup_refresh_error("thread")
                logger.warning("Could not refresh sent thread %s: %s", thread_id, exc)
        self._judge_new_anchors()
        Metrics.set_followup_threads(threads.counts())
        now = self._clock()
        due = [
            t for t in threads.in_state(WAITING_FOR_THEM) if would_propose(t, self.threshold, now)
        ]
        Metrics.followup_due.set(len(due))
        for thread in due:
            self._tell_due(thread)

    def _tell_due(self, thread: TrackedThread) -> None:
        if self._on_due is None:
            return
        try:
            self._on_due(thread)
        except Exception as exc:  # noqa: BLE001 - told again at the next refresh
            logger.warning("Could not ask for the follow-up of %s: %s", thread.thread_id, exc)

    def _judge_new_anchors(self) -> None:
        # Without an answer a thread is never offered: an unjudged mail is not assumed to wait.
        if self._judge is None:
            return
        threads = self._store.threads
        pending = [
            t
            for t in threads.in_state(WAITING_FOR_THEM)
            if t.anchor is not None and t.jev_asked_for != t.anchor.message_id
        ]
        for thread in pending[:MAX_JUDGED_PER_REFRESH]:
            anchor = thread.anchor
            try:
                text = self._mail.sent_text(anchor.message_id)
                probability = self._judge.expects_answer(anchor.subject, text)
            except Exception as exc:  # noqa: BLE001 - asked again at the next refresh
                Metrics.mark_followup_refresh_error("judge")
                logger.warning("Could not judge sent mail %s: %s", anchor.message_id, exc)
                continue
            threads.update(
                thread.thread_id,
                lambda current, a=anchor.message_id, p=probability: with_answer(current, a, p),
            )
            Metrics.mark_followup_judged(probability >= self.threshold)

    def _read(self, thread_id: str, mine: frozenset[str], activated_at: datetime) -> None:
        threads = self._store.threads
        snapshot = self._mail.thread_snapshot(thread_id)
        now = self._clock()
        if snapshot is None:
            if threads.get(thread_id) is not None:
                threads.update(thread_id, lambda current: close(current, DELETED, now))
            return
        derived = derive(snapshot, mine, activated_at)
        due_at = None
        if derived.state == WAITING_FOR_THEM:
            local_due = add_business_days(derived.anchor.sent_at, self._after_days, self._timezone)
            due_at = local_due.astimezone(UTC)
        # Merged with the row as it is now, not as it was before the network call: the chat may
        # have dismissed the thread meanwhile.
        threads.update(thread_id, lambda current: track(current, snapshot, derived, due_at, now))

    def _expired(self, thread: TrackedThread) -> bool:
        return thread.anchor is None or self._clock() - thread.anchor.sent_at > MAX_TRACKING
