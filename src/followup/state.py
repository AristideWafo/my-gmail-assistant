from dataclasses import replace
from datetime import datetime, timedelta, tzinfo

from src.domain import (
    CLOSED,
    IGNORED,
    WAITING_FOR_THEM,
    FollowUpAnchor,
    ThreadSnapshot,
    ThreadState,
    TrackedThread,
    canonical_address,
)
from src.triage.rules import is_automated_sender

NOT_MINE = "not_mine"
BEFORE_ACTIVATION = "before_activation"
NO_RECIPIENT = "no_recipient"
AUTOMATED_RECIPIENT = "automated_recipient"
TOO_MANY_RECIPIENTS = "too_many_recipients"
ANSWERED = "answered"
BOUNCED = "bounced"
DELETED = "deleted"
EXPIRED = "expired"
DISMISSED = "dismissed"
ANSWERED_ELSEWHERE = "answered_elsewhere"
# Beyond it a mail is a group mail: who answered is per person, out of scope, and every
# recipient would have to be searched before each follow-up.
MAX_RECIPIENTS = 5
USEFUL = "useful"
NOT_USEFUL = "not_useful"

# proposal_state values.
NONE = "none"
OFFERED = "offered"
SENT = "sent"
SNOOZED = "snoozed"
HANDED_OFF = "handed_off"
EXPIRED_OFFER = "expired"


def derive(
    snapshot: ThreadSnapshot, my_addresses: frozenset[str], activated_at: datetime
) -> ThreadState:
    """Who the thread waits for, read from its headers alone; `my_addresses` are canonical.

    Every doubt resolves to "not waiting": a follow-up to someone who already answered costs
    more than one that was never proposed.
    """
    messages = snapshot.messages
    # Thread order, not dates: a message whose date is unreadable must still count as later.
    mine = [i for i, message in enumerate(messages) if message.from_me and not message.automated]
    if not mine:
        return ThreadState(IGNORED, NOT_MINE)
    position = mine[-1]
    sent = messages[position]
    if sent.sent_at < activated_at:
        return ThreadState(IGNORED, BEFORE_ACTIVATION)
    anchor = FollowUpAnchor(
        message_id=sent.id,
        sent_at=sent.sent_at,
        to=tuple(a for a in sent.to if canonical_address(a) not in my_addresses),
        cc=tuple(a for a in sent.cc if canonical_address(a) not in my_addresses),
        subject=sent.subject,
        message_id_header=sent.message_id_header,
        references=tuple(m.message_id_header for m in messages[: position + 1] if m.message_id_header),
    )
    later = messages[position + 1 :]
    if any(message.bounce for message in later):
        return ThreadState(CLOSED, BOUNCED, anchor)
    if any(not message.from_me and not message.automated for message in later):
        return ThreadState(CLOSED, ANSWERED, anchor)
    recipients = anchor.to + anchor.cc
    if not recipients:
        return ThreadState(IGNORED, NO_RECIPIENT, anchor)
    if all(is_automated_sender(address) for address in recipients):
        return ThreadState(IGNORED, AUTOMATED_RECIPIENT, anchor)
    if len(recipients) > MAX_RECIPIENTS:
        return ThreadState(IGNORED, TOO_MANY_RECIPIENTS, anchor)
    return ThreadState(WAITING_FOR_THEM, anchor=anchor)


def add_business_days(start: datetime, days: int, timezone: tzinfo) -> datetime:
    """Same local time `days` weekdays later; a start on a weekend counts from the next Monday."""
    local = start.astimezone(timezone)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    added = 0
    while added < days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            added += 1
    return local


def track(
    existing: TrackedThread | None,
    snapshot: ThreadSnapshot,
    derived: ThreadState,
    due_at: datetime | None,
    now: datetime,
) -> TrackedThread:
    """The stored thread after a fresh read: what belongs to the old anchor goes with it."""
    same_anchor = (
        existing is not None
        and existing.anchor is not None
        and derived.anchor is not None
        and existing.anchor.message_id == derived.anchor.message_id
    )
    if same_anchor and existing.proposal_state == DISMISSED and derived.state == WAITING_FOR_THEM:
        derived = ThreadState(CLOSED, DISMISSED, derived.anchor)
    if same_anchor:
        base = existing
    else:
        base = TrackedThread(
            thread_id=snapshot.thread_id,
            history_id="",
            state=derived.state,
            updated_at=now,
            proposals_count=existing.proposals_count if existing is not None else 0,
        )
    return replace(
        base,
        history_id=snapshot.history_id,
        state=derived.state,
        reason=derived.reason,
        anchor=derived.anchor,
        due_at=due_at,
        updated_at=now,
    )


def close(thread: TrackedThread, reason: str, now: datetime) -> TrackedThread:
    return replace(thread, state=CLOSED, reason=reason, updated_at=now)


def would_propose(thread: TrackedThread, threshold: float, now: datetime) -> bool:
    """A follow-up is due: still waiting, judged to expect an answer, late, not rated as
    unwanted, and either never offered or postponed until now."""
    never_offered = thread.proposal_state == NONE and thread.proposals_count == 0
    postponed = (
        thread.proposal_state == SNOOZED
        and thread.snoozed_until is not None
        and thread.snoozed_until <= now
    )
    return (
        thread.state == WAITING_FOR_THEM
        and thread.expects_answer is not None
        and thread.expects_answer >= threshold
        and thread.due_at is not None
        and thread.due_at <= now
        and thread.verdict != NOT_USEFUL
        and (never_offered or postponed)
    )


def with_answer(
    thread: TrackedThread | None, anchor_id: str, probability: float
) -> TrackedThread | None:
    """Stores JEV's answer unless the anchor changed while it was asked."""
    if thread is None or thread.anchor is None or thread.anchor.message_id != anchor_id:
        return None
    return replace(thread, expects_answer=probability, jev_asked_for=anchor_id)
