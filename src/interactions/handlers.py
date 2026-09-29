import logging

from src.formatting import truncate
from src.gateways.telegram_bot import Button, CallbackEvent, Event, ReplyEvent, TelegramBot
from src.gmail.client import GmailClient
from src.interactions.callbacks import (
    CANCEL,
    FEEDBACK,
    SEND,
    Callback,
    draft_buttons,
    parse_callback,
)
from src.observability.metrics import Metrics
from src.storage.decision_store import DecisionRecord, DecisionStore

logger = logging.getLogger(__name__)

PREVIEW_BODY_CHARS = 500
REPLY_HINT = "Réponds directement à une alerte pour créer un brouillon."
DRAFT_FAILED = "Impossible de créer le brouillon Gmail, réessaie plus tard."
NO_BUTTONS_HINT = "Envoie-le depuis Gmail."
FEEDBACK_ACKS = {
    "valid": "Merci, alerte validée",
    "false_urgent": "Noté : faux urgent",
    "false_spam": "Noté : spam",
}
UNKNOWN_MAIL = "Mail inconnu"
UNKNOWN_ACTION = "Action non reconnue"
ALREADY_SENT = "Déjà envoyé"
SENT = "Envoyé"
SEND_FAILED = "Envoi incertain : vérifie tes Envoyés dans Gmail avant de renvoyer."
CANCELLED = "Brouillon conservé dans Gmail"


class InteractionHandler:
    def __init__(self, store: DecisionStore, bot: TelegramBot, gmail: GmailClient) -> None:
        self._store = store
        self._bot = bot
        self._gmail = gmail

    def dispatch(self, event: Event) -> None:
        if isinstance(event, CallbackEvent):
            self._on_callback(event)
        elif isinstance(event, ReplyEvent):
            self._on_reply(event)

    def _on_callback(self, event: CallbackEvent) -> None:
        callback = parse_callback(event.data)
        if callback is None:
            logger.warning("Ignoring unrecognised callback on message %s", event.message_id)
            self._answer(event, UNKNOWN_ACTION)
            return
        handlers = {FEEDBACK: self._on_feedback, SEND: self._on_send, CANCEL: self._on_cancel}
        handlers[callback.action](event, callback)

    def _on_feedback(self, event: CallbackEvent, callback: Callback) -> None:
        if not self._store.record_feedback(callback.target, callback.verdict):
            self._answer(event, UNKNOWN_MAIL)
            return
        Metrics.mark_feedback(callback.verdict)
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
            if self._gmail.send_draft(callback.target) is None:
                raise RuntimeError("Gmail is not configured")
        except Exception:
            logger.exception("Failed to send Gmail draft from Telegram")
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
        logger.warning("Rejected draft action on Telegram message %s", event.message_id)
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
            draft = self._gmail.create_draft(
                record.thread_id,
                record.sender,
                record.subject,
                text,
                in_reply_to=record.message_id_header,
            )
        except Exception:
            logger.exception("Failed to create Gmail draft for email %s", record.message_id)
            return None
        return (draft or {}).get("id")

    def _answer(self, event: CallbackEvent, text: str) -> None:
        try:
            self._bot.answer_callback(event.callback_id, text)
        except Exception as exc:  # noqa: BLE001 - the ack is cosmetic, the action already happened
            logger.warning("Failed to answer Telegram callback: %s", exc)

    def _clear(self, message_id: int) -> None:
        try:
            self._bot.clear_buttons(message_id)
        except Exception as exc:  # noqa: BLE001 - stale buttons are guarded by stored state
            logger.warning("Failed to clear Telegram buttons on message %s: %s", message_id, exc)

    def _notify(
        self, text: str, buttons: list[list[Button]] | None = None, reply_to: int | None = None
    ) -> int | None:
        try:
            return self._bot.send_message(text, buttons=buttons, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001 - the draft, if any, is already safe in Gmail
            logger.warning("Failed to send Telegram message: %s", exc)
            return None


def _offer_key(draft_id: str) -> str:
    return f"bot_draft:{draft_id}"


def format_draft_preview(record: DecisionRecord, text: str) -> str:
    return (
        f"✉️ Brouillon prêt\nÀ : {record.sender}\nObjet : {record.subject}\n\n"
        f"{truncate(text, PREVIEW_BODY_CHARS)}"
    )
