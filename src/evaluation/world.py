from dataclasses import dataclass
from datetime import UTC, datetime

from src.domain import EmailMessage, ThreadMessage, ThreadSnapshot

_OPERATORS = {"from": "sender", "subject": "subject"}


@dataclass(frozen=True)
class WorldMail:
    id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    date: str
    to: tuple[str, ...] = ()
    from_me: bool = False

    def email(self) -> EmailMessage:
        return EmailMessage(
            id=self.id,
            thread_id=self.thread_id,
            sender=self.sender,
            subject=self.subject,
            snippet=self.body[:100],
            body=self.body,
            received_at=self.date,
        )


class InMemoryMail:
    """An invented mailbox a scenario is played against: it reads, and has nothing to write."""

    is_configured = True

    def __init__(self, mails: list[WorldMail]) -> None:
        self._mails = sorted(mails, key=lambda mail: mail.date)

    def check_connection(self) -> str:
        return "in-memory mailbox"

    def fetch_message(self, message_id: str, full_body: bool = False) -> EmailMessage | None:
        return next((mail.email() for mail in self._mails if mail.id == message_id), None)

    def thread_snapshot(self, thread_id: str) -> ThreadSnapshot | None:
        mails = [mail for mail in self._mails if mail.thread_id == thread_id]
        if not mails:
            return None
        return ThreadSnapshot(
            thread_id,
            history_id="1",
            messages=tuple(
                ThreadMessage(
                    id=mail.id,
                    sender=mail.sender,
                    to=mail.to,
                    cc=(),
                    sent_at=datetime.fromisoformat(mail.date).replace(tzinfo=UTC),
                    subject=mail.subject,
                    from_me=mail.from_me,
                )
                for mail in mails
            ),
        )

    def search(self, query: str, limit: int) -> list[EmailMessage]:
        """Words must all be found; `from:` and `subject:` look in that field; any other
        operator (dates, labels) is ignored, so a query can only find too much, never too little
        for a reason the scenario did not write."""
        wanted = [_matcher(term) for term in query.lower().split()]
        found = [mail for mail in self._mails if all(matches(mail) for matches in wanted)]
        return [mail.email() for mail in reversed(found)][:limit]


def _matcher(term: str):
    operator, _, value = term.partition(":")
    if value and operator in _OPERATORS:
        field = _OPERATORS[operator]
        return lambda mail: value.strip('"') in getattr(mail, field).lower()
    if value:
        return lambda mail: True
    word = term.strip('"()')
    return lambda mail: word in f"{mail.sender} {mail.subject} {mail.body}".lower()
