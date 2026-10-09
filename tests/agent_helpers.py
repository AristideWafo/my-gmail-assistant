from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.agent.profiles import AgentPorts
from src.domain import EmailMessage
from src.storage import SqliteDecisionStore
from tests.fakes import FakeMail
from tests.followup_helpers import message, snapshot

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
SECRET = "SECRET-OF-ANOTHER-THREAD"
WRITES = ("archive_message", "label_message", "create_draft", "send_draft")


def email(
    message_id, thread_id, subject="Devis", body="Où en est le devis ?", sender="jean@example.com"
):
    return EmailMessage(
        id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        snippet=body[:40],
        body=body,
        received_at="2026-10-05",
    )


@dataclass
class SpyMail(FakeMail):
    """Records every call; a write is recorded instead of carried out."""

    calls: list[tuple[str, tuple]] = field(default_factory=list)

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if name.startswith("_") or name in ("calls", "called") or not callable(value):
            return value
        calls = super().__getattribute__("calls")

        def recorded(*args, **kwargs):
            calls.append((name, args))
            return None if name in WRITES else value(*args, **kwargs)

        return recorded

    def called(self, name):
        return [args for called, args in self.calls if called == name]

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in WRITES]


@dataclass
class FakeJudge:
    answer: float = 0.9
    is_configured: bool = True
    asked: list = field(default_factory=list)

    def probability(self, state, question, yes, no):
        self.asked.append((state, question, yes, no))
        return self.answer


def world():
    """Thread t1 (two mails) and thread t2, whose text must never reach a run bound to t1."""
    mail = SpyMail(
        unread=[
            email("m1", "t1"),
            email("m2", "t1", body="Je relance pour le devis."),
            email("m9", "t2", subject="Salaires", body=SECRET),
        ],
        threads=[
            snapshot(
                message("m1", sender="jean@example.com", to=("me@example.com",)), message("m2")
            ),
            snapshot(message("m9", sender="rh@example.com"), thread_id="t2"),
        ],
    )
    store = SqliteDecisionStore(":memory:", clock=lambda: NOW)
    judge = FakeJudge()
    return AgentPorts(mail=mail, store=store, judge=judge, clock=lambda: NOW)
