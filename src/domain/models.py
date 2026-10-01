from dataclasses import dataclass, field

VERDICTS = ("valid", "false_urgent", "false_spam", "missed_urgent", "wrong_archive")
FEEDBACK_ORIGINS = ("alert", "review")


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    body: str
    sender_domain: str = ""
    received_at: str = ""
    message_id_header: str = ""


@dataclass
class TriageResult:
    urgency: str
    category: str
    confidence: float
    # Which stage decided: "rule", "jev" or "heuristic".
    source: str = ""


@dataclass
class LLMAnalysis:
    summary: str = ""
    draft: str = ""
    entities: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionRecord:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    excerpt: str
    urgency: str
    category: str
    confidence: float
    route: str
    created_at: str
    chat_message_id: int | None = None
    message_id_header: str = ""
    source: str = ""


@dataclass(frozen=True)
class Correction:
    sender: str
    subject: str
    excerpt: str
    predicted_urgency: str
    predicted_category: str
    verdict: str


@dataclass(frozen=True)
class FeedbackTally:
    route: str
    source: str
    verdict: str
    count: int


@dataclass(frozen=True)
class CallbackEvent:
    callback_id: str
    message_id: int
    data: str


@dataclass(frozen=True)
class ReplyEvent:
    message_id: int
    reply_to_message_id: int
    text: str


@dataclass(frozen=True)
class CommandEvent:
    message_id: int
    name: str
    args: str = ""


Button = tuple[str, str]
ChatEvent = CallbackEvent | ReplyEvent | CommandEvent
