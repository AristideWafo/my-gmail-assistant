import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

from src.domain import CLOSED, CallbackEvent, TrackedThread
from src.followup.offers import ANCHOR_CHANGED, offer_expiry, recheck, recipients, settle
from src.followup.state import (
    DISMISSED,
    EXPIRED_OFFER,
    HANDED_OFF,
    OFFERED,
    SENT,
    SNOOZED,
)
from src.interactions.callbacks import Callback
from src.observability.metrics import Metrics
from src.ports import DecisionStore, MailProvider

logger = logging.getLogger(__name__)

SNOOZE = timedelta(days=3)
UNKNOWN_OFFER = "Proposition inconnue ou déjà traitée"
DISABLED = "Relances désactivées : rien n'a été envoyé"
EXPIRED = "Proposition expirée : rien n'a été envoyé"
CANNOT_VERIFY = "Impossible de vérifier le fil dans Gmail : rien n'a été envoyé, réessaie plus tard"
ANSWERED_MEANWHILE = "Une réponse est arrivée entre-temps : relance annulée"
THREAD_CHANGED = "Le fil a changé depuis la proposition : relance annulée"
DRAFT_FAILED = "Impossible de créer le brouillon Gmail : rien n'a été envoyé"
SENT_ACK = "Relance envoyée"
SEND_FAILED = "Envoi incertain : vérifie tes Envoyés dans Gmail avant de renvoyer."
SNOOZED_ACK = "Relance reportée de 3 jours"
DISMISSED_ACK = "Pas de relance pour ce fil"
HANDED_OFF_ACK = "Brouillon créé dans Gmail : à toi de l'envoyer"


class FollowUpActions:
    """What the buttons of a follow-up offer do. Sending is irreversible: every check runs
    again at the press, and the send itself happens at most once."""

    def __init__(
        self,
        store: DecisionStore,
        mail: MailProvider,
        answer: Callable[[CallbackEvent, str], None],
        clear: Callable[[int], None],
        enabled: bool,
        timezone: tzinfo = UTC,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._mail = mail
        self._answer = answer
        self._clear = clear
        self._enabled = enabled
        self._timezone = timezone
        self._clock = clock

    def handle(self, event: CallbackEvent, callback: Callback) -> None:
        thread = self._store.threads.get(callback.target)
        # Callback data is client-supplied: only the offer this bot made, pressed on the very
        # message that made it, is honoured, and its text and recipients come from the store.
        if (
            thread is None
            or thread.proposal_state != OFFERED
            or thread.offered_on != event.message_id
        ):
            logger.warning("Rejected follow-up action on chat message %s", event.message_id)
            Metrics.mark_followup_proposal("rejected")
            self._answer(event, UNKNOWN_OFFER)
            return
        actions = {
            "send": self._send,
            "snooze": self._snooze,
            "dismiss": self._dismiss,
            "edit": self._edit,
        }
        actions[callback.verdict](event, thread)

    def _send(self, event: CallbackEvent, thread: TrackedThread) -> None:
        sent_key = f"followup_sent:{thread.thread_id}:{thread.proposals_count}"
        # Checked before any draft: a press redelivered after a crash must neither add a second
        # draft nor claim a send that may never have happened.
        if self._store.get_state(sent_key) is not None:
            self._finish(event, thread, SENT, SEND_FAILED, "duplicate")
            return
        if not self._still_valid(event, thread):
            return
        draft_id = self._draft(event, thread)
        if draft_id is None:
            return
        # Claimed before calling Gmail: a crash or a redelivered press can at worst skip the
        # follow-up, never send it twice.
        self._store.set_state(sent_key, draft_id)
        try:
            if not self._mail.send_draft(draft_id):
                raise RuntimeError("mail provider is not configured")
        except Exception:
            logger.exception("Failed to send the follow-up of thread %s", thread.thread_id)
            self._finish(event, thread, SENT, SEND_FAILED, "send_failed")
            return
        self._finish(event, thread, SENT, SENT_ACK, "sent")

    def _edit(self, event: CallbackEvent, thread: TrackedThread) -> None:
        if not self._still_valid(event, thread):
            return
        if self._draft(event, thread) is not None:
            self._finish(event, thread, HANDED_OFF, HANDED_OFF_ACK, "handed_off")

    def _snooze(self, event: CallbackEvent, thread: TrackedThread) -> None:
        until = self._clock() + SNOOZE
        self._finish(event, thread, SNOOZED, SNOOZED_ACK, "snoozed", snoozed_until=until)

    def _dismiss(self, event: CallbackEvent, thread: TrackedThread) -> None:
        self._finish(
            event, thread, DISMISSED, DISMISSED_ACK, "dismissed", state=CLOSED, reason=DISMISSED
        )

    def _still_valid(self, event: CallbackEvent, thread: TrackedThread) -> bool:
        if not self._enabled:
            self._answer(event, DISABLED)
            return False
        now = self._clock()
        if thread.offered_at is None or now > offer_expiry(thread.offered_at, self._timezone):
            self._finish(event, thread, EXPIRED_OFFER, EXPIRED, "expired")
            return False
        try:
            reason = recheck(self._mail, thread)
        except Exception as exc:  # noqa: BLE001 - unknown means no
            logger.warning("Could not recheck thread %s before a follow-up: %s", thread.thread_id, exc)
            self._answer(event, CANNOT_VERIFY)
            return False
        if reason is None:
            return True
        self._store.threads.update(thread.thread_id, lambda current: settle(current, reason, now))
        if reason == ANCHOR_CHANGED:
            self._finish(event, thread, EXPIRED_OFFER, THREAD_CHANGED, ANCHOR_CHANGED)
        else:
            Metrics.mark_followup_proposal(reason)
            self._answer(event, ANSWERED_MEANWHILE)
            self._clear(event.message_id)
        return False

    def _draft(self, event: CallbackEvent, thread: TrackedThread) -> str | None:
        anchor = thread.anchor
        to, cc = recipients(anchor)
        try:
            draft_id = self._mail.create_draft(
                thread.thread_id,
                ", ".join(to),
                anchor.subject,
                thread.proposal_text,
                in_reply_to=anchor.message_id_header,
                cc=cc,
                references=anchor.references,
            )
        except Exception:
            logger.exception("Failed to create the follow-up draft of thread %s", thread.thread_id)
            draft_id = None
        if draft_id is None:
            self._answer(event, DRAFT_FAILED)
        return draft_id

    def _finish(
        self,
        event: CallbackEvent,
        thread: TrackedThread,
        proposal_state: str,
        text: str,
        outcome: str,
        **changes,
    ) -> None:
        now = self._clock()
        anchor_id = thread.anchor.message_id

        def apply(current: TrackedThread | None) -> TrackedThread | None:
            if current is None or current.anchor is None or current.anchor.message_id != anchor_id:
                return None
            return replace(current, proposal_state=proposal_state, updated_at=now, **changes)

        self._store.threads.update(thread.thread_id, apply)
        Metrics.mark_followup_proposal(outcome)
        self._answer(event, text)
        self._clear(event.message_id)
