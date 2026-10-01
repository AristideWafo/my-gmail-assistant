from dataclasses import dataclass, field

from src.bootstrap import Components
from src.domain import Button, ChatEvent, EmailMessage, LLMAnalysis, TriageResult
from src.storage import SqliteDecisionStore


@dataclass
class FakeMail:
    is_configured: bool = True
    unread: list[EmailMessage] = field(default_factory=list)
    labels: list[tuple[str, str]] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)

    def check_connection(self) -> str:
        return "fake mail"

    def fetch_unread(self) -> list[EmailMessage]:
        return list(self.unread)

    def fetch_history(self) -> list[EmailMessage]:
        return []

    def fetch_message(self, message_id: str) -> EmailMessage | None:
        return next((email for email in self.unread if email.id == message_id), None)

    def archive_message(self, message_id: str) -> None:
        self.archived.append(message_id)

    def label_message(self, message_id: str, label_name: str) -> None:
        self.labels.append((message_id, label_name))

    def create_draft(
        self, thread_id: str, to: str, subject: str, body: str, in_reply_to: str = ""
    ) -> str | None:
        return "draft-1"

    def send_draft(self, draft_id: str) -> bool:
        return True


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
        self, text: str, buttons: list[list[Button]] | None = None, reply_to: int | None = None
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
