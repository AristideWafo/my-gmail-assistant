from dataclasses import dataclass

from src.agent.tools import ToolRefused
from src.domain import EmailMessage
from src.ports import MailProvider

# One message for "does not exist" and "not yours to read": the difference would tell a run
# which ids exist outside its reach.
NOT_READABLE = "No such mail within reach."


@dataclass(frozen=True)
class MailScope:
    """Which threads a run may read. It is fixed by what started the run, never by what the
    run went on to read."""

    thread_ids: frozenset[str] | None

    @property
    def whole_mailbox(self) -> bool:
        return self.thread_ids is None

    def allows(self, thread_id: str) -> bool:
        return self.thread_ids is None or thread_id in self.thread_ids

    def read(self, mail: MailProvider, message_id: str) -> EmailMessage:
        email = mail.fetch_message(message_id, full_body=True)
        if email is None or not self.allows(email.thread_id):
            raise ToolRefused(NOT_READABLE)
        return email


WHOLE_MAILBOX = MailScope(None)


def only_thread(thread_id: str) -> MailScope:
    # A mail without a thread id is parsed with an empty one: an empty scope id would match
    # every such mail.
    if not thread_id:
        raise ValueError("a run cannot be bound to an empty thread id")
    return MailScope(frozenset({thread_id}))
