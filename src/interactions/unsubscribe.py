import hashlib
import json
import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit

from src.domain import EmailMessage
from src.interactions.callbacks import unsubscribe_buttons
from src.observability.metrics import Metrics
from src.ports import ChatInbox, DecisionStore

logger = logging.getLogger(__name__)

STREAK_WINDOW = timedelta(days=30)
ONE_CLICK_POST = "list-unsubscribe=one-click"
_BRACKETED_URI_RE = re.compile(r"<\s*([^<>\s]+)\s*>")


def one_click_url(email: EmailMessage) -> str | None:
    """The RFC 8058 one-click link, or None when the mail offers no trustworthy one."""
    # The headers are the sender's: only believe them when the sender is who it claims to be.
    if not email.dmarc_pass:
        return None
    if email.list_unsubscribe_post.replace(" ", "").lower() != ONE_CLICK_POST:
        return None
    for uri in _BRACKETED_URI_RE.findall(email.list_unsubscribe):
        if uri.lower().startswith("https://"):
            return uri
    return None


def offer_id(sender: str) -> str:
    # Callback data is limited to 64 bytes and an address may be longer.
    return hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]


def offered_key(sender: str) -> str:
    return f"unsub_offered:{sender}"


def offer_key(identifier: str) -> str:
    return f"unsub:{identifier}"


def format_offer(sender: str, archived: int, url: str) -> str:
    return (
        f"📭 {sender}\n"
        f"{archived} mails archivés en {STREAK_WINDOW.days} jours, aucun gardé.\n"
        f"Se désabonner ? La demande part vers {urlsplit(url).hostname}."
    )


class UnsubscribeProposer:
    def __init__(self, store: DecisionStore, chat: ChatInbox, min_archived: int) -> None:
        self._store = store
        self._chat = chat
        self._min_archived = min_archived

    def consider(self, email: EmailMessage) -> bool:
        """Offers, at most once per sender, to unsubscribe from mail that is always archived."""
        url = one_click_url(email)
        if url is None:
            return False
        sender = email.sender.lower()
        if self._store.get_state(offered_key(sender)) is not None:
            return False
        archived = self._store.archived_streak(sender, STREAK_WINDOW)
        if archived < self._min_archived:
            return False
        identifier = offer_id(sender)
        buttons = unsubscribe_buttons(identifier)
        if buttons is None:
            return False
        message_id = self._chat.send_message(format_offer(sender, archived, url), buttons=buttons)
        # The link stays here: callback data is client-supplied and must only carry the id.
        self._store.set_state(
            offer_key(identifier),
            json.dumps({"url": url, "sender": sender, "offered_on": message_id}),
        )
        self._store.set_state(offered_key(sender), identifier)
        Metrics.mark_unsubscribe("offered")
        return True
