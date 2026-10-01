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
    # True only when the receiving provider itself vouches for the From address.
    dmarc_pass: bool = False
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""


@dataclass
class TriageResult:
    urgency: str
    category: str
    confidence: float
    # Which stage decided: "rule", "jev" or "heuristic".
    source: str = ""
    # Probability that a person expects a written reply; None when the question was not asked.
    needs_reply: float | None = None


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
