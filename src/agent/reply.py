import re
from dataclasses import dataclass
from datetime import timedelta

from src.domain import ThreadSnapshot
from src.triage.rules import is_automated_sender

SEND_REPLY = "send_reply"
PROPOSAL_LIFETIME = timedelta(hours=48)
MAX_REPLY_CHARS = 3000
# Telegram refuses a longer message, and a proposal cut to fit would be confirmed unread.
MAX_PREVIEW_CHARS = 4096
# One address and nothing else: what goes into a To header comes from a mail's own headers.
_PLAIN_ADDRESS_RE = re.compile(r"^[^@\s<>,;:\"']+@[^@\s<>,;:\"']+\.[^@\s<>,;:\"']+$")


@dataclass(frozen=True)
class ReplyTarget:
    """Where a reply in a thread goes, read from the thread and never from the model."""

    to: tuple[str, ...]
    subject: str
    in_reply_to: str
    references: tuple[str, ...]
    last_message_id: str


def reply_target(snapshot: ThreadSnapshot) -> ReplyTarget | None:
    """None when the thread has nobody a person would answer."""
    written = [m for m in snapshot.messages if not m.automated and not m.bounce]
    if not written:
        return None
    theirs = [m for m in written if not m.from_me]
    # Their last message is answered; a thread holding only my mails is continued to those I
    # wrote to.
    to = (theirs[-1].sender,) if theirs else written[-1].to
    to = tuple(a for a in to if _PLAIN_ADDRESS_RE.match(a) and not is_automated_sender(a))
    if not to:
        return None
    answered = theirs[-1] if theirs else written[-1]
    return ReplyTarget(
        to=to,
        subject=answered.subject,
        in_reply_to=answered.message_id_header,
        references=tuple(m.message_id_header for m in snapshot.messages if m.message_id_header),
        last_message_id=snapshot.messages[-1].id,
    )


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
            f"Objet : {payload['subject'] or '(sans objet)'}",
            "",
            "Texte envoyé tel quel :",
            payload["body"],
            "",
            "Réponds à ce message pour le faire modifier.",
        ]
    )
