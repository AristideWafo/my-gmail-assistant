import logging

from src.domain import (
    Button,
    CallbackEvent,
    ChatEvent,
    CommandEvent,
    DecisionRecord,
    ReplyEvent,
)
from src.formatting import truncate
from src.interactions.callbacks import (
    CANCEL,
    FEEDBACK,
    REVIEW,
    SEND,
    Callback,
    draft_buttons,
    parse_callback,
)
from src.interactions.commands import CommandRouter
from src.interactions.review import ReviewCommand
from src.observability.metrics import Metrics
from src.ports import ChatInbox, DecisionStore, MailProvider

logger = logging.getLogger(__name__)

PREVIEW_BODY_CHARS = 500
REPLY_HINT = "Réponds directement à une alerte pour créer un brouillon."
DRAFT_FAILED = "Impossible de créer le brouillon Gmail, réessaie plus tard."
NO_BUTTONS_HINT = "Envoie-le depuis Gmail."
FEEDBACK_ACKS = {
    "valid": "Merci, validé",
    "false_urgent": "Noté : faux urgent",
    "false_spam": "Noté : spam",
    "missed_urgent": "Noté : urgent raté",
    "wrong_archive": "Noté : à garder",
}
FEEDBACK_ORIGIN = {FEEDBACK: "alert", REVIEW: "review"}
UNKNOWN_MAIL = "Mail inconnu"
UNKNOWN_ACTION = "Action non reconnue"
ALREADY_SENT = "Déjà envoyé"
SENT = "Envoyé"
SEND_FAILED = "Envoi incertain : vérifie tes Envoyés dans Gmail avant de renvoyer."
CANCELLED = "Brouillon conservé dans Gmail"


class InteractionHandler:
    def __init__(self, store: DecisionStore, chat: ChatInbox, mail: MailProvider) -> None:
        self._store = store
        self._chat = chat
        self._mail = mail
        self.commands = CommandRouter(chat)
        self.commands.register(
            "review", "mails non alertés à vérifier, ex. /review 5", ReviewCommand(store, chat).run
        )

    def dispatch(self, event: ChatEvent) -> None:
        if isinstance(event, CallbackEvent):
            self._on_callback(event)
        elif isinstance(event, ReplyEvent):
            self._on_reply(event)
        elif isinstance(event, CommandEvent):
            self._on_command(event)

    def _on_command(self, event: CommandEvent) -> None:
        command_key = f"cmd:{event.message_id}"
        if self._store.get_state(command_key) is not None:
            Metrics.mark_chat_command(self.commands.label(event.name), "duplicate")
            return
        # Claimed before running: a redelivered update must not replay a command that already
        # sent half of its messages.
        self._store.set_state(command_key, event.name)
        self.commands.dispatch(event)

    def _on_callback(self, event: CallbackEvent) -> None:
        callback = parse_callback(event.data)
        if callback is None:
            logger.warning("Ignoring unrecognised callback on message %s", event.message_id)
            self._answer(event, UNKNOWN_ACTION)
            return
        handlers = {
            FEEDBACK: self._on_feedback,
            REVIEW: self._on_feedback,
            SEND: self._on_send,
            CANCEL: self._on_cancel,
        }
        handlers[callback.action](event, callback)

    def _on_feedback(self, event: CallbackEvent, callback: Callback) -> None:
        record = self._store.get(callback.target)
        origin = FEEDBACK_ORIGIN[callback.action]
        if record is None or not self._store.record_feedback(
            callback.target, callback.verdict, origin
        ):
            self._answer(event, UNKNOWN_MAIL)
            return
        Metrics.mark_feedback(callback.verdict, record.route)
        self._answer(event, FEEDBACK_ACKS[callback.verdict])
        self._clear(event.message_id)

    def _on_send(self, event: CallbackEvent, callback: Callback) -> None:
        if not self._offered_here(event, callback):
            return
        sent_key = f"draft_sent:{callback.target}"
        if self._store.get_state(sent_key) is not None:
            Metrics.mark_chat_reply("duplicate")
            self._answer(event, ALREADY_SENT)
            self._clear(event.message_id)
            return
        # Sending is irreversible and callbacks are delivered at-least-once: claim the send
        # before calling Gmail, so a crash or timeout can at worst skip it, never send twice.
        self._store.set_state(sent_key, "1")
        try:
            if not self._mail.send_draft(callback.target):
                raise RuntimeError("Gmail is not configured")
        except Exception:
            logger.exception("Failed to send Gmail draft from chat")
            Metrics.mark_chat_reply("send_failed")
            self._answer(event, SEND_FAILED)
        else:
            Metrics.mark_chat_reply("sent")
            self._answer(event, SENT)
        self._clear(event.message_id)

    def _on_cancel(self, event: CallbackEvent, callback: Callback) -> None:
        if not self._offered_here(event, callback):
            return
        Metrics.mark_chat_reply("cancelled")
        self._clear(event.message_id)
        self._answer(event, CANCELLED)

    def _offered_here(self, event: CallbackEvent, callback: Callback) -> bool:
        # Callback data is client-supplied: only a draft this bot offered, pressed on the very
        # preview that offered it, may be sent or cancelled.
        offered_on = self._store.get_state(_offer_key(callback.target))
        if offered_on is not None and offered_on == str(event.message_id):
            return True
        logger.warning("Rejected draft action on chat message %s", event.message_id)
        Metrics.mark_chat_reply("rejected")
        self._answer(event, UNKNOWN_ACTION)
        return False

    def _on_reply(self, event: ReplyEvent) -> None:
        reply_key = f"reply:{event.message_id}"
        if self._store.get_state(reply_key) is not None:
            Metrics.mark_chat_reply("duplicate")
            return
        record = self._store.find_by_chat_message(event.reply_to_message_id)
        if record is None:
            Metrics.mark_chat_reply("unknown_target")
            self._notify(REPLY_HINT, reply_to=event.message_id)
            return
        draft_id = self._create_draft(record, event.text)
        if draft_id is None:
            Metrics.mark_chat_reply("draft_failed")
            self._notify(DRAFT_FAILED, reply_to=event.message_id)
            return
        # A crash before this point only duplicates a harmless draft on redelivery.
        self._store.set_state(reply_key, draft_id)
        Metrics.mark_chat_reply("drafted")
        buttons = draft_buttons(draft_id)
        preview = format_draft_preview(record, event.text)
        if buttons is None:
            preview = f"{preview}\n\n{NO_BUTTONS_HINT}"
        preview_id = self._notify(preview, buttons=buttons, reply_to=event.message_id)
        if buttons is not None and preview_id is not None:
            self._store.set_state(_offer_key(draft_id), str(preview_id))

    def _create_draft(self, record: DecisionRecord, text: str) -> str | None:
        try:
            return self._mail.create_draft(
                record.thread_id,
                record.sender,
                record.subject,
                text,
                in_reply_to=record.message_id_header,
            )
        except Exception:
            logger.exception("Failed to create Gmail draft for email %s", record.message_id)
            return None

    def _answer(self, event: CallbackEvent, text: str) -> None:
        try:
            self._chat.answer_callback(event.callback_id, text)
        except Exception as exc:  # noqa: BLE001 - the ack is cosmetic, the action already happened
            logger.warning("Failed to answer chat callback: %s", exc)

    def _clear(self, message_id: int) -> None:
        try:
            self._chat.clear_buttons(message_id)
        except Exception as exc:  # noqa: BLE001 - stale buttons are guarded by stored state
            logger.warning("Failed to clear chat buttons on message %s: %s", message_id, exc)

    def _notify(
        self, text: str, buttons: list[list[Button]] | None = None, reply_to: int | None = None
    ) -> int | None:
        try:
            return self._chat.send_message(text, buttons=buttons, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001 - the draft, if any, is already safe in Gmail
            logger.warning("Failed to send chat message: %s", exc)
            return None


def _offer_key(draft_id: str) -> str:
    return f"bot_draft:{draft_id}"


def format_draft_preview(record: DecisionRecord, text: str) -> str:
    return (
        f"✉️ Brouillon prêt\nÀ : {record.sender}\nObjet : {record.subject}\n\n"
        f"{truncate(text, PREVIEW_BODY_CHARS)}"
    )
