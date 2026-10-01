import copy
from collections.abc import Callable
from dataclasses import dataclass

from src.gmail.text_cleaning import truncate_words
from src.triage.attention import ATTENTION_QUESTIONS
from src.triage.engine import NEEDS_REPLY_QUESTION, JevClassifier
from src.workflow import attention_reasons, route_for, route_with_attention

Answers = dict[str, dict]
SIGNAL_THRESHOLD = 0.5

ROUTE_OF_ACTION = {"alert": "llm", "keep": "label", "archive": "reject"}
ACTION_QUESTION = {
    "type": "choice",
    "instructions": (
        "What should the recipient's mail assistant do with this email? Judge deadlines "
        "against `today` and `received_at`."
    ),
    "criteria": {
        "alert": "Notify the recipient now: something must be done today or tomorrow (a human "
        "message with a same-day or next-day deadline, an administrative or financial deadline, "
        "a production failure, a security incident). Bulk, marketing or scam mail is never alert",
        "keep": "Leave it in the inbox, labeled: a person wrote, or the information is worth "
        "reading, but nothing has to be done today",
        "archive": "Remove it from the inbox: newsletter, promotion, scam, receipt, automated "
        "notice with nothing to do",
    },
}


def _yes_no(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


# Candidate questions, not asked in production: a question moves to the classifier once the lab
# has shown it reliable on real mail and a feature consumes it.
SIGNAL_QUESTIONS = {
    "needs_reply": NEEDS_REPLY_QUESTION,
    "asks_for_meeting": _yes_no(
        "Does this email propose, schedule, move or remind a meeting, call or appointment "
        "involving the recipient?",
        "A meeting, call or appointment is involved",
        "None",
    ),
    "has_deadline": _yes_no(
        "Does this email state a date or time by which the recipient must do something?",
        "An explicit or clearly implied deadline for the recipient",
        "No deadline for the recipient",
    ),
    "has_payment_due": _yes_no(
        "Does this email say the recipient has to pay something or fix a failed payment?",
        "Money is owed or a payment must be fixed by the recipient",
        "No payment expected from the recipient, including receipts of payments already made",
    ),
    "contains_commitment": _yes_no(
        "Does the sender commit to doing something later for the recipient (send, come back, "
        "deliver, call)?",
        "The sender promises a future action",
        "No promise of a future action by the sender",
    ),
}
KNOWN_QUESTIONS = {**SIGNAL_QUESTIONS, **ATTENTION_QUESTIONS}


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    # Turns the request production sends into the one this variant sends.
    prepare: Callable[[dict], dict]
    route: Callable[[Answers, float], str]
    signals: tuple[str, ...] = ()
    # Whether production would put the mail forward on these answers, at the given threshold.
    put_forward: Callable[[Answers, float], bool] | None = None
    # What is kept of a long body; production keeps its first 1000 words.
    cut: Callable[[str], str] = truncate_words


def _unchanged(request: dict) -> dict:
    return request


def _current_route(answers: Answers, low_confidence_threshold: float) -> str:
    return route_for(JevClassifier.parse_answers({"answers": answers}), low_confidence_threshold)


def _ask_action(request: dict) -> dict:
    questions = {"action": ACTION_QUESTION, "category": request["questions"]["category"]}
    return {**request, "questions": questions}


def _action_route(answers: Answers, _: float) -> str:
    return ROUTE_OF_ACTION[answers["action"]["choice"]]


def _add_signals(request: dict) -> dict:
    questions = {**request["questions"], **copy.deepcopy(SIGNAL_QUESTIONS)}
    return {**request, "questions": questions}


def _add_attention(request: dict) -> dict:
    questions = {
        **request["questions"],
        "needs_reply": NEEDS_REPLY_QUESTION,
        **copy.deepcopy(ATTENTION_QUESTIONS),
    }
    return {**request, "questions": questions}


def _attention_route(answers: Answers, low_confidence_threshold: float) -> str:
    triage = JevClassifier.parse_answers({"answers": answers})
    return route_with_attention(triage, low_confidence_threshold, SIGNAL_THRESHOLD)


def _put_forward(answers: Answers, threshold: float) -> bool:
    return bool(attention_reasons(JevClassifier.parse_answers({"answers": answers}), threshold))


def _cut(head_words: int, tail_words: int = 0) -> Callable[[str], str]:
    return lambda text: truncate_words(text, head_words, tail_words)


def _truncation(name: str, description: str, head_words: int, tail_words: int = 0) -> Variant:
    return Variant(name, description, _unchanged, _current_route, cut=_cut(head_words, tail_words))


VARIANTS = {
    variant.name: variant
    for variant in (
        Variant(
            "current",
            "urgency and category, routed as production does",
            _unchanged,
            _current_route,
        ),
        Variant(
            "direct-action",
            "one question choosing alert, keep or archive, instead of urgency",
            _ask_action,
            _action_route,
        ),
        Variant(
            "signals",
            "current questions plus the candidate yes/no questions, in the same call",
            _add_signals,
            _current_route,
            signals=tuple(SIGNAL_QUESTIONS),
        ),
        Variant(
            "attention",
            "current questions plus reply expected and the three attention questions; a mail "
            "put forward is not archived",
            _add_attention,
            _attention_route,
            signals=("needs_reply", *ATTENTION_QUESTIONS),
            put_forward=_put_forward,
        ),
        _truncation("head-700-tail-300", "body cut to its first 700 and last 300 words", 700, 300),
        _truncation("head-150", "body cut to its first 150 words", 150),
        _truncation("head-100-tail-50", "body cut to its first 100 and last 50 words", 100, 50),
    )
}
# What `--source recent` compares by default: production and the shorter bodies.
TRUNCATION_VARIANTS = ("current", "head-700-tail-300", "head-150", "head-100-tail-50")
# The default elsewhere: a shorter body changes nothing on short test mails.
QUESTION_VARIANTS = tuple(name for name in VARIANTS if name not in TRUNCATION_VARIANTS[1:])
