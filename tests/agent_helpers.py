from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.agent.profiles import AgentPorts
from src.domain import (
    WAITING_FOR_THEM,
    EmailMessage,
    FollowUpAnchor,
    TrackedThread,
    TriageResult,
)
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
    """Thread t1 (two mails) and thread t2, of which nothing may reach a run bound to t1. The
    store already holds what the assistant recorded about both."""
    hostile = email("m2", "t1", subject="x" * 200_000, body="Je relance.", sender="y" * 100_000)
    other = email("m9", "t2", subject=f"Salaires {SECRET}", body=SECRET, sender=f"{SECRET}@rh.fr")
    mail = SpyMail(
        unread=[email("m1", "t1"), hostile, other],
        threads=[
            snapshot(
                message("m1", sender="jean@example.com", to=("me@example.com",)), message("m2")
            ),
            snapshot(message("m9", sender="rh@example.com"), thread_id="t2"),
        ],
    )
    store = SqliteDecisionStore(":memory:", clock=lambda: NOW)
    store.record_decision(other, TriageResult("low", "personnel", 0.9), "label")
    anchor = FollowUpAnchor("m9", NOW - timedelta(days=4), (f"{SECRET}@rh.fr",), (), SECRET)
    store.threads.save(TrackedThread("t2", "1", WAITING_FOR_THEM, NOW, anchor=anchor, due_at=NOW))
    return AgentPorts(mail=mail, store=store, judge=FakeJudge(), clock=lambda: NOW)
