import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from src.domain import ThreadSnapshot, canonical_address
from src.triage.rules import is_automated_sender

SEND_REPLY = "send_reply"
PROPOSAL_LIFETIME = timedelta(hours=48)
MAX_REPLY_CHARS = 3000
# Telegram refuses a longer message, and a proposal cut to fit would be confirmed unread.
MAX_PREVIEW_CHARS = 4096
# One plain ASCII address and nothing else: what goes into a To header comes from a mail's own
# headers, and a character that does not show could make it another address than the one read.
_PLAIN_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
# Format, control, private-use, unassigned and surrogate characters: none of them shows.
_UNSEEN_CATEGORIES = frozenset({"Cf", "Cc", "Co", "Cn", "Cs"})


@dataclass(frozen=True)
class ReplyTarget:
    """Where a reply in a thread goes, read from the thread and never from the model."""

    to: tuple[str, ...]
    subject: str
    in_reply_to: str
    references: tuple[str, ...]
    last_message_id: str


def reply_target(snapshot: ThreadSnapshot, my_addresses: frozenset[str]) -> ReplyTarget | None:
    """None when the thread has nobody a person would answer; `my_addresses` are canonical."""
    written = [m for m in snapshot.messages if not m.automated and not m.bounce]
    if not written:
        return None
    theirs = [m for m in written if not m.from_me]
    # Their last message is answered; a thread holding only my mails is continued to those I
    # wrote to.
    to = (theirs[-1].sender,) if theirs else written[-1].to
    to = tuple(
        address
        for address in to
        if _PLAIN_ADDRESS_RE.match(address)
        and not is_automated_sender(address)
        and canonical_address(address) not in my_addresses
    )
    if not to:
        return None
    answered = theirs[-1] if theirs else written[-1]
    return ReplyTarget(
        to=to,
        # Final here, so that the subject shown is the subject sent.
        subject=_reply_subject(answered.subject),
        in_reply_to=answered.message_id_header,
        references=tuple(m.message_id_header for m in snapshot.messages if m.message_id_header),
        last_message_id=snapshot.messages[-1].id,
    )


def has_unseen_characters(text: str) -> bool:
    """True when the text holds something a screen does not show: what is confirmed must be
    all of what is sent."""
    return any(
        unicodedata.category(character) in _UNSEEN_CATEGORIES and character not in "\n\t"
        for character in text
    )


def shown_length(text: str) -> int:
    """Length as Telegram counts it, in UTF-16 units: an emoji is two."""
    return len(text.encode("utf-16-le")) // 2


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def reply_payload(thread_id: str, target: ReplyTarget, body: str) -> dict:
    return {
        "thread_id": thread_id,
        "to": list(target.to),
        "subject": target.subject,
        "body": body,
        "in_reply_to": target.in_reply_to,
        "references": list(target.references),
        "last_message_id": target.last_message_id,
    }


def format_proposal(payload: dict) -> str:
    return "\n".join(
        [
            "✉️ Réponse proposée",
            f"À : {', '.join(payload['to'])}",
            f"Objet : {payload['subject']}",
            "",
            "Texte envoyé tel quel :",
            payload["body"],
            "",
            "Réponds à ce message pour le faire modifier.",
        ]
    )
