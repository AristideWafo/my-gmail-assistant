import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, tzinfo

from src.domain import CLOSED, WAITING_FOR_THEM, Button, FollowUpAnchor, TrackedThread
from src.followup.compose import ComposedFollowUps
from src.followup.state import (
    ANSWERED_ELSEWHERE,
    DELETED,
    EXPIRED_OFFER,
    OFFERED,
    add_business_days,
    derive,
    would_propose,
)
from src.followup.template import follow_up_text
from src.observability.metrics import Metrics
from src.ports import ChatInbox, DecisionStore, MailProvider
from src.scheduling import ProactiveBudget

logger = logging.getLogger(__name__)

# Two weekdays, not 48 hours: an offer made on Friday must still be there on Monday.
OFFER_LIFETIME_DAYS = 2
OFFERS_KEY_PREFIX = "followup_offers:"
ANCHOR_CHANGED = "anchor_changed"
# A follow-up offered on Friday evening would sit unread until Monday, then look stale.
FRIDAY_EVENING_HOUR = 17
_EVER = datetime(1970, 1, 1, tzinfo=UTC)


def recipients(anchor: FollowUpAnchor) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """To and Cc of the follow-up: those of my mail, Cc moved to To when To held only me."""
    if anchor.to:
        return anchor.to, anchor.cc
    return anchor.cc, ()


def offer_expiry(offered_at: datetime, timezone: tzinfo) -> datetime:
    return add_business_days(offered_at, OFFER_LIFETIME_DAYS, timezone)


def recheck(mail: MailProvider, thread: TrackedThread) -> str | None:
    """Why the thread no longer awaits this follow-up, read fresh from Gmail; None if it still
    does. Raises when Gmail cannot tell: the caller must then not offer nor send."""
    snapshot = mail.thread_snapshot(thread.thread_id)
    if snapshot is None:
        return DELETED
    # Activation does not matter here: the anchor was accepted when it was first tracked.
    derived = derive(snapshot, mail.my_addresses(), _EVER)
    if derived.anchor is None or derived.anchor.message_id != thread.anchor.message_id:
        return ANCHOR_CHANGED
    if derived.state != WAITING_FOR_THEM:
        return derived.reason
    to, cc = recipients(thread.anchor)
    if any(mail.has_message_from(address, thread.anchor.sent_at) for address in to + cc):
        return ANSWERED_ELSEWHERE
    return None


def settle(thread: TrackedThread | None, reason: str, now: datetime) -> TrackedThread | None:
    """What a recheck found: an answer or a deletion closes the thread; a new anchor of mine is
    left to the next refresh, which tracks it with its own clock."""
    if thread is None or reason == ANCHOR_CHANGED:
        return None
    return replace(thread, state=CLOSED, reason=reason, updated_at=now)


ADVICE_LABELS = {"wait": "attendre encore", "drop": "ne pas relancer"}


def format_advice(advice: str) -> str:
    """The agent's `kind: reason`, as a line of the offer; empty when it advises to send."""
    kind, _, reason = advice.partition(": ")
    if kind not in ADVICE_LABELS or not reason:
        return ""
    return f"Avis de l'assistant : {ADVICE_LABELS[kind]} — {reason}"


def format_offer(thread: TrackedThread, text: str, timezone: tzinfo, advice: str = "") -> str:
    to, cc = recipients(thread.anchor)
    sent = thread.anchor.sent_at.astimezone(timezone).strftime("%d/%m")
    lines = [
        "🔁 Relance proposée",
        f"À : {', '.join(to)}",
    ]
    if cc:
        lines.append(f"Cc : {', '.join(cc)}")
    lines += [
        f"Objet : {thread.anchor.subject or '(sans objet)'}",
        f"Sans réponse depuis le {sent}.",
        "",
        "Texte envoyé tel quel :",
        text,
    ]
    if format_advice(advice):
        lines += ["", format_advice(advice)]
    return "\n".join(lines)


class FollowUpOffers:
    """Offers, silently and within the day's budget, the follow-ups that became due."""

    def __init__(
        self,
        store: DecisionStore,
        mail: MailProvider,
        chat: ChatInbox,
        budget: ProactiveBudget,
        buttons: Callable[[str], list[list[Button]] | None],
        threshold: float,
        timezone: tzinfo,
        hour: int,
        daily_max: int,
        signature: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        composed: ComposedFollowUps | None = None,
    ) -> None:
        # None: every follow-up carries the fixed text.
        self._composed = composed
        self._store = store
        self._mail = mail
        self._chat = chat
        self._budget = budget
        self._buttons = buttons
        self._threshold = threshold
        self._timezone = timezone
        self._hour = hour
        self._daily_max = daily_max
        self._signature = signature
        self._clock = clock

    def run(self) -> int:
        """Returns how many follow-ups were offered."""
        self._expire_old_offers()
        local = self._clock().astimezone(self._timezone)
        if not self._in_window(local):
            return 0
        left = min(self._daily_max - self._offered_today(local), self._budget.available())
        offered = 0
        for thread in self._candidates():
            if offered >= left:
                break
            if self._offer(thread):
                offered += 1
        return offered

    def _in_window(self, local: datetime) -> bool:
        if local.weekday() >= 5 or local.hour < self._hour:
            return False
        return not (local.weekday() == 4 and local.hour >= FRIDAY_EVENING_HOUR)

    def _candidates(self) -> list[TrackedThread]:
        now = self._clock()
        return [
            thread
            for thread in self._store.threads.in_state(WAITING_FOR_THEM)
            if would_propose(thread, self._threshold, now)
        ]

    def _offer(self, thread: TrackedThread) -> bool:
        threads = self._store.threads
        if self._composed is not None and self._composed.still_writing(thread):
            return False
        try:
            reason = recheck(self._mail, thread)
            if reason is not None:
                now = self._clock()
                threads.update(thread.thread_id, lambda current: settle(current, reason, now))
                Metrics.mark_followup_proposal(reason)
                return False
            text, advice = self._text(thread)
            message_id = self._chat.send_message(
                format_offer(thread, text, self._timezone, advice),
                buttons=self._buttons(thread.thread_id),
                silent=True,
            )
        except Exception as exc:  # noqa: BLE001 - offered at the next run instead
            Metrics.mark_followup_refresh_error("offer")
            logger.warning("Could not offer a follow-up on thread %s: %s", thread.thread_id, exc)
            return False
        now = self._clock()
        anchor_id = thread.anchor.message_id

        def offer(current: TrackedThread | None) -> TrackedThread | None:
            if current is None or current.anchor is None or current.anchor.message_id != anchor_id:
                return None
            return replace(
                current,
                proposal_state=OFFERED,
                proposal_text=text,
                offered_on=message_id,
                offered_at=now,
                proposals_count=current.proposals_count + 1,
                updated_at=now,
            )

        try:
            recorded = threads.update(thread.thread_id, offer)
        except Exception as exc:  # noqa: BLE001 - handled below like an offer that did not stick
            logger.warning("Could not record the follow-up offered on %s: %s", thread.thread_id, exc)
            recorded = None
        if recorded is None:
            # Buttons nobody can honour must not stay on screen.
            self._withdraw(message_id)
            return False
        self._budget.spend()
        local = now.astimezone(self._timezone)
        self._store.set_state(self._day_key(local), str(self._offered_today(local) + 1))
        Metrics.mark_followup_proposal("offered")
        return True

    def _text(self, thread: TrackedThread) -> tuple[str, str]:
        written = self._composed.text_for(thread) if self._composed is not None else None
        if written is not None:
            Metrics.mark_followup_text("agent")
            return written, self._composed.advice_for(thread)
        Metrics.mark_followup_text("template")
        text = follow_up_text(
            thread.anchor,
            self._mail.sent_text(thread.anchor.message_id),
            self._signature,
            self._timezone,
        )
        return text, ""

    def _withdraw(self, message_id: int) -> None:
        try:
            self._chat.clear_buttons(message_id)
        except Exception as exc:  # noqa: BLE001 - a press is rejected anyway, the state was not recorded
            logger.warning("Could not withdraw follow-up offer %s: %s", message_id, exc)

    def _expire_old_offers(self) -> None:
        now = self._clock()
        threads = self._store.threads
        for thread in threads.in_state(WAITING_FOR_THEM):
            if (
                thread.proposal_state == OFFERED
                and thread.offered_at is not None
                and now > offer_expiry(thread.offered_at, self._timezone)
            ):
                threads.update(
                    thread.thread_id,
                    lambda current: None
                    if current is None or current.proposal_state != OFFERED
                    else replace(current, proposal_state=EXPIRED_OFFER, updated_at=now),
                )
                Metrics.mark_followup_proposal("expired")

    def _offered_today(self, local: datetime) -> int:
        return int(self._store.get_state(self._day_key(local)) or 0)

    @staticmethod
    def _day_key(local: datetime) -> str:
        return f"{OFFERS_KEY_PREFIX}{local.date().isoformat()}"
