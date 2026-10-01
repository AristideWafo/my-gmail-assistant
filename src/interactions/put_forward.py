import logging
from datetime import timedelta

from src.domain import CommandEvent, DecisionRecord
from src.formatting import truncate
from src.interactions.callbacks import put_forward_buttons
from src.ports import ChatInbox, DecisionStore, MailProvider
from src.workflow import attention_reasons

logger = logging.getLogger(__name__)

PENDING_WINDOW = timedelta(days=7)
DAILY_ITEMS = 5
COMMAND_ITEMS = 10
EXCERPT_CHARS = 200
LAST_SENT_KEY = "put_forward_list:last"
NOTHING_PENDING = "Rien à voir : aucun mail mis en avant en attente."
REASON_LABELS = {
    "personal_event": "événement ou rendez-vous",
    "service_change": "coupure ou changement annoncé",
    "personal_deadline": "à faire avant une date",
    "needs_reply": "réponse attendue",
}


def format_item(record: DecisionRecord, reasons: tuple[str, ...], position: int, total: int) -> str:
    lines = [
        f"👀 À voir {position}/{total}",
        f"De : {record.sender}",
        f"Objet : {record.subject}",
    ]
    if reasons:
        lines.append("Pourquoi : " + ", ".join(REASON_LABELS.get(name, name) for name in reasons))
    excerpt = truncate(" ".join(record.excerpt.split()), EXCERPT_CHARS)
    return "\n".join(lines) + (f"\n\n{excerpt}" if excerpt else "")


def format_header(shown: int, postponed: int, older: int, interactive: bool) -> str:
    parts = [f"👀 À voir : {_count(shown + postponed, 'nouveau mail', 'nouveaux mails')}"]
    if postponed:
        parts.append(f"{postponed} reporté(s) à demain")
    if older:
        parts.append(f"{older} plus ancien(s) en attente")
    # The command only answers when the listener runs.
    if interactive and (postponed or older):
        parts.append("/avoir pour tout voir")
    return " · ".join(parts)


def format_remainder(hidden: int) -> str:
    return f"… et {hidden} plus ancien(s) non affiché(s)."


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


class PutForwardList:
    """The mails put forward that still wait: not rated, and still in the inbox."""

    def __init__(
        self,
        store: DecisionStore,
        chat: ChatInbox,
        mail: MailProvider,
        threshold: float,
    ) -> None:
        self._store = store
        self._chat = chat
        self._mail = mail
        self._threshold = threshold

    def run(self, event: CommandEvent) -> None:
        pending = self._pending()
        if not pending:
            self._chat.send_message(NOTHING_PENDING, reply_to=event.message_id)
            return
        shown = pending[-COMMAND_ITEMS:]
        for position, record in enumerate(shown, start=1):
            self._send(record, position, len(shown), interactive=True, silent=False)
        if len(pending) > len(shown):
            self._chat.send_message(format_remainder(len(pending) - len(shown)))

    def send_daily(self, interactive: bool) -> int:
        """Sends, without sound, what was put forward since the previous list; returns how many."""
        pending = self._pending()
        last_sent = self._store.get_state(LAST_SENT_KEY) or ""
        fresh = [record for record in pending if record.created_at > last_sent]
        if not fresh:
            return 0
        shown = fresh[:DAILY_ITEMS]
        header = format_header(
            len(shown), len(fresh) - len(shown), len(pending) - len(fresh), interactive
        )
        self._chat.send_message(header, silent=True)
        for position, record in enumerate(shown, start=1):
            self._send(record, position, len(shown), interactive, silent=True)
            # Moved mail by mail: after a failure the next list resumes at the first mail that
            # did not go out, and what exceeds the cap stays new for tomorrow.
            self._store.set_state(LAST_SENT_KEY, record.created_at)
        return len(shown)

    def _pending(self) -> list[DecisionRecord]:
        records = self._store.put_forward_pending(PENDING_WINDOW)
        return [record for record in records if self._still_in_inbox(record)]

    def _still_in_inbox(self, record: DecisionRecord) -> bool:
        # Archiving or deleting the mail in Gmail is how it is handled without the buttons.
        try:
            return self._mail.in_inbox(record.message_id)
        except Exception as exc:  # noqa: BLE001 - an unknown state must not hide the mail
            logger.warning("Could not read the state of %s: %s", record.message_id, exc)
            return True

    def _send(
        self, record: DecisionRecord, position: int, total: int, interactive: bool, silent: bool
    ) -> None:
        reasons = attention_reasons(record, self._threshold)
        buttons = put_forward_buttons(record.message_id) if interactive else None
        self._chat.send_message(
            format_item(record, reasons, position, total), buttons=buttons, silent=silent
        )
