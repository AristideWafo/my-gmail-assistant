import json

from src.agent.schema import arguments, integer, text
from src.agent.scope import NOT_READABLE, MailScope
from src.agent.tools import Tool, ToolRefused
from src.domain import EmailMessage, ToolSpec
from src.formatting import truncate
from src.ports import MailProvider

MAX_SEARCH_RESULTS = 10
MAX_THREAD_MESSAGES = 8
MAX_BODY_CHARS = 3000
MAX_ID_CHARS = 64
MAX_QUERY_CHARS = 200
MAX_HEADER_CHARS = 300
MAX_RECIPIENTS = 10


def mail_tools(mail: MailProvider, scope: MailScope) -> list[Tool]:
    """Reading tools only. Searching exists only for a run allowed the whole mailbox."""

    def read_mail(args: dict) -> str:
        return _encode(_full(scope.read(mail, args["message_id"])))

    def read_thread(args: dict) -> str:
        # Refused before the provider is asked anything about a thread out of reach.
        if not scope.allows(args["thread_id"]):
            raise ToolRefused(NOT_READABLE)
        snapshot = mail.thread_snapshot(args["thread_id"])
        # The provider's word is checked too: the scope must hold whatever it returns.
        if snapshot is None or not scope.allows(snapshot.thread_id):
            raise ToolRefused(NOT_READABLE)
        messages = []
        for message in snapshot.messages[-MAX_THREAD_MESSAGES:]:
            try:
                email = scope.read(mail, message.id)
            except ToolRefused:
                continue
            messages.append(
                {
                    **_full(email),
                    "to": [short(address) for address in message.to[:MAX_RECIPIENTS]],
                    "from_me": message.from_me,
                }
            )
        return _encode({"thread_id": snapshot.thread_id, "messages": messages})

    def search_mail(args: dict) -> str:
        limit = args.get("limit", MAX_SEARCH_RESULTS)
        return _encode([_summary(email) for email in mail.search(args["query"], limit)])

    tools = [
        Tool(
            ToolSpec(
                "read_mail",
                "Returns one mail in full: sender, subject, date and body.",
                arguments(message_id=text("Id of the mail.", MAX_ID_CHARS)),
            ),
            read_mail,
        ),
        Tool(
            ToolSpec(
                "read_thread",
                f"Returns the last {MAX_THREAD_MESSAGES} mails of a thread in full, oldest first.",
                arguments(thread_id=text("Id of the thread.", MAX_ID_CHARS)),
            ),
            read_thread,
        ),
    ]
    if scope.whole_mailbox:
        tools.append(
            Tool(
                ToolSpec(
                    "search_mail",
                    "Searches the mailbox with a Gmail query (from:, to:, subject:, after:, "
                    "newer_than:, plain words). Returns ids, senders, subjects and snippets, "
                    "not bodies.",
                    arguments(
                        optional=("limit",),
                        query=text("Gmail search query.", MAX_QUERY_CHARS),
                        limit=integer("Most results wanted.", 1, MAX_SEARCH_RESULTS),
                    ),
                ),
                search_mail,
            )
        )
    return tools


def short(value: str) -> str:
    """Headers are the sender's text too: a subject or an address is cut like a body is."""
    return truncate(value, MAX_HEADER_CHARS)


def _summary(email: EmailMessage) -> dict:
    return {
        "message_id": short(email.id),
        "thread_id": short(email.thread_id),
        "from": short(email.sender),
        "subject": short(email.subject),
        "date": short(email.received_at),
        "snippet": short(email.snippet),
    }


def _full(email: EmailMessage) -> dict:
    summary = _summary(email)
    del summary["snippet"]
    return {**summary, "body": truncate(email.body or email.snippet, MAX_BODY_CHARS)}


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
