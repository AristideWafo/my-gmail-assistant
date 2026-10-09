import logging
from collections.abc import Callable

from src.agent.reply import SEND_REPLY
from src.domain import ACTION_PENDING, CallbackEvent, PendingAction
from src.interactions.callbacks import Callback
from src.observability.metrics import Metrics
from src.ports import DecisionStore, MailProvider

logger = logging.getLogger(__name__)

UNKNOWN_PROPOSAL = "Proposition inconnue, expirée ou déjà traitée"
CANCELLED = "Proposition annulée : rien n'a été envoyé"
CANNOT_VERIFY = "Impossible de vérifier le fil dans Gmail : rien n'a été envoyé, réessaie plus tard"
THREAD_CHANGED = "Le fil a reçu un message depuis la proposition : rien n'a été envoyé"
DRAFT_FAILED = "Impossible de créer le brouillon Gmail : rien n'a été envoyé"
SENT = "Envoyé"
SEND_FAILED = "Envoi incertain : vérifie tes Envoyés dans Gmail avant de renvoyer."


class ProposalActions:
    """What the buttons of a proposal do. Nothing here involves a model: what is sent is the
    payload that was stored and shown, to the recipients read from the thread."""

    def __init__(
        self,
        store: DecisionStore,
        mail: MailProvider,
        answer: Callable[[CallbackEvent, str], None],
        clear: Callable[[int], None],
    ) -> None:
        self._actions = store.pending_actions
        self._mail = mail
        self._answer = answer
        self._clear = clear

    def handle(self, event: CallbackEvent, callback: Callback) -> None:
        if callback.verdict == "cancel":
            cancelled = self._actions.cancel(callback.target, event.message_id)
            self._close(
                event,
                "cancelled" if cancelled else "rejected",
                CANCELLED if cancelled else UNKNOWN_PROPOSAL,
            )
            return
        known = self._actions.get(callback.target)
        # Checked before the action is taken for good: a Gmail hiccup must leave it pressable.
        if _is_open_reply(known) and not self._still_valid(event, known):
            return
        # The one gate: only the proposal this bot made, pressed on the message that showed it,
        # before it expired and still holding what was shown, gets through, and once.
        action = self._actions.begin(callback.target, event.message_id)
        if action is None or action.kind != SEND_REPLY:
            if action is not None:
                self._actions.finish(action.id, succeeded=False)
            logger.warning("Rejected proposal action on chat message %s", event.message_id)
            self._close(event, "rejected", UNKNOWN_PROPOSAL)
            return
        self._actions.finish(action.id, succeeded=self._send_reply(event, action))

    def _still_valid(self, event: CallbackEvent, action: PendingAction) -> bool:
        try:
            snapshot = self._mail.thread_snapshot(action.payload["thread_id"])
        except Exception as exc:  # noqa: BLE001 - unknown means no
            logger.warning("Could not recheck the thread of proposal %s: %s", action.id, exc)
            self._answer(event, CANNOT_VERIFY)
            return False
        last = snapshot.messages[-1].id if snapshot is not None and snapshot.messages else None
        if last == action.payload["last_message_id"]:
            return True
        # What was written answers a thread that no longer ends where it did.
        self._actions.cancel(action.id, event.message_id)
        self._close(event, "thread_changed", THREAD_CHANGED)
        return False

    def _send_reply(self, event: CallbackEvent, action: PendingAction) -> bool:
        payload = action.payload
        try:
            draft_id = self._mail.create_draft(
                payload["thread_id"],
                ", ".join(payload["to"]),
                payload["subject"],
                payload["body"],
                in_reply_to=payload["in_reply_to"],
                references=payload["references"],
            )
        except Exception:
            logger.exception("Failed to create the draft of proposal %s", action.id)
            draft_id = None
        if draft_id is None:
            self._close(event, "draft_failed", DRAFT_FAILED)
            return False
        try:
            if not self._mail.send_draft(draft_id):
                raise RuntimeError("mail provider is not configured")
        except Exception:
            logger.exception("Failed to send proposal %s", action.id)
            self._close(event, "send_failed", SEND_FAILED)
            return False
        self._close(event, "sent", SENT)
        return True

    def _close(self, event: CallbackEvent, outcome: str, text: str) -> None:
        Metrics.mark_agent_proposal(outcome)
        self._answer(event, text)
        self._clear(event.message_id)


def _is_open_reply(action: PendingAction | None) -> bool:
    return action is not None and action.kind == SEND_REPLY and action.state == ACTION_PENDING
