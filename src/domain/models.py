from dataclasses import dataclass, field
from datetime import datetime

VERDICTS = (
    "valid",
    "false_urgent",
    "false_spam",
    "missed_urgent",
    "wrong_archive",
    "missed_important",
    "false_important",
)
FEEDBACK_ORIGINS = ("alert", "review", "list")


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
    # True only when the receiving provider itself vouches for the From address.
    dmarc_pass: bool = False
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""



@dataclass(frozen=True)
class ThreadMessage:
    """What a follow-up needs to know about one message of a thread: never its body."""

    id: str
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    sent_at: datetime
    subject: str = ""
    message_id_header: str = ""
    # The mailbox owner wrote it: Gmail's SENT label, or one of the owner's sending addresses.
    from_me: bool = False
    # An auto-reply, a bulk or list mail, or a bounce, as its headers declare it.
    automated: bool = False
    # A delivery failure report: the message never reached its recipients.
    bounce: bool = False


@dataclass(frozen=True)
class ThreadSnapshot:
    thread_id: str
    history_id: str
    messages: tuple[ThreadMessage, ...]


@dataclass(frozen=True)
class ThreadRef:
    thread_id: str
    history_id: str

@dataclass
class TriageResult:
    urgency: str
    category: str
    confidence: float
    # Which stage decided: "rule", "jev" or "heuristic".
    source: str = ""
    # Probability that a person expects a written reply; None when the question was not asked.
    needs_reply: float | None = None
    # Probability of each attention question that was asked and answered, by question name.
    signals: dict[str, float] = field(default_factory=dict)


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
    needs_reply: float | None = None
    signals: dict[str, float] = field(default_factory=dict)
    put_forward: bool = False


@dataclass(frozen=True)
class RatedDecision:
    record: DecisionRecord
    verdict: str
    rated_at: str


@dataclass(frozen=True)
class RuleCandidate:
    sender: str
    urgency: str
    category: str
    count: int


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
