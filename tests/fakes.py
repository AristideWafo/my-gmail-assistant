from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from src.bootstrap import Components
from src.domain import (
    AgentMessage,
    AgentTurn,
    Button,
    ChatEvent,
    EmailMessage,
    LLMAnalysis,
    ThreadRef,
    ThreadSnapshot,
    ToolSpec,
    TriageResult,
)
from src.errors import AgentModelError
from src.storage import SqliteDecisionStore


@dataclass
class FakeMail:
    is_configured: bool = True
    unread: list[EmailMessage] = field(default_factory=list)
    labels: list[tuple[str, str]] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    addresses: frozenset[str] = frozenset({"me@example.com"})
    threads: list[ThreadSnapshot] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    answered_elsewhere: set[str] = field(default_factory=set)

    def check_connection(self) -> str:
        return "fake mail"

    def fetch_unread(self) -> list[EmailMessage]:
        return list(self.unread)

    def fetch_history(self) -> list[EmailMessage]:
        return []

    def fetch_message(self, message_id: str, full_body: bool = False) -> EmailMessage | None:
        return next((email for email in self.unread if email.id == message_id), None)

    def in_inbox(self, message_id: str) -> bool:
        return message_id not in self.archived

    def archive_message(self, message_id: str) -> None:
        self.archived.append(message_id)

    def label_message(self, message_id: str, *label_names: str) -> None:
        self.labels.extend((message_id, name) for name in label_names)

    def create_draft(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        cc: Sequence[str] = (),
        references: Sequence[str] = (),
    ) -> str | None:
        return "draft-1"

    def send_draft(self, draft_id: str) -> bool:
        return True

    def my_addresses(self) -> frozenset[str]:
        return self.addresses

    def sent_threads(self, newer_than_days: int, limit: int) -> list[ThreadRef]:
        return [ThreadRef(snapshot.thread_id, snapshot.history_id) for snapshot in self.threads][:limit]

    def thread_snapshot(self, thread_id: str) -> ThreadSnapshot | None:
        return next((s for s in self.threads if s.thread_id == thread_id), None)

    def sent_text(self, message_id: str) -> str:
        return self.texts.get(message_id, "")

    def has_message_from(self, address: str, after: datetime) -> bool:
        return address in self.answered_elsewhere


@dataclass
class FakeClassifier:
    result: TriageResult = field(default_factory=lambda: TriageResult("low", "personnel", 0.9))
    is_configured: bool = True

    def check_connection(self) -> str:
        return "fake classifier"

    def classify(self, email: EmailMessage) -> TriageResult:
        return self.result


@dataclass
class FakeAnalyzer:
    is_configured: bool = True

    def check_connection(self) -> str:
        return "fake analyzer"

    def analyze(self, email: EmailMessage, want_draft: bool, want_entities: bool) -> LLMAnalysis:
        return LLMAnalysis(summary="summary")


@dataclass
class FakeAgentModel:
    """Plays the turns it was given, in order, whatever it is shown; records what it was shown."""

    script: list[AgentTurn | Exception] = field(default_factory=list)
    is_configured: bool = True
    seen: list[tuple[str, tuple[AgentMessage, ...], tuple[ToolSpec, ...]]] = field(
        default_factory=list
    )

    def check_connection(self) -> str:
        return "fake agent model"

    def step(
        self, system: str, messages: Sequence[AgentMessage], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        self.seen.append((system, tuple(messages), tuple(tools)))
        if not self.script:
            raise AgentModelError("the script is over")
        turn = self.script.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


@dataclass
class FakeChannel:
    name: str = "fake"
    is_configured: bool = True
    interactive: bool = False
    sent: list[str] = field(default_factory=list)

    def check_connection(self) -> str:
        return f"{self.name} reachable"

    def send(self, text: str, buttons: list[list[Button]] | None = None) -> int | None:
        self.sent.append(text)
        return len(self.sent) if self.interactive else None

    def update(
        self, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        self.sent[message_id - 1] = text


@dataclass
class FakeChat:
    is_configured: bool = True
    inbound_authorized: bool = True

    def check_connection(self) -> str:
        return "ok"

    def get_updates(self, offset: int | None) -> tuple[list[ChatEvent], int | None]:
        return [], offset

    def send_message(
        self,
        text: str,
        buttons: list[list[Button]] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> int:
        return 1

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        pass

    def clear_buttons(self, message_id: int) -> None:
        pass


def fake_components(**overrides) -> Components:
    defaults = {
        "mail": FakeMail(),
        "classifier": FakeClassifier(),
        "analyzer": FakeAnalyzer(),
        "channels": (FakeChannel(),),
        "chat": FakeChat(),
        "store": SqliteDecisionStore(":memory:"),
    }
    return Components(**{**defaults, **overrides})
