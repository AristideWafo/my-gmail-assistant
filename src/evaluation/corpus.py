import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from src.domain import EmailMessage
from src.errors import ConfigurationError
from src.evaluation.dataset import Case
from src.evaluation.variants import SIGNAL_QUESTIONS
from src.gmail.text_cleaning import extract_domain
from src.triage.rules import RuleSet

CORPUS_PATH = Path(__file__).with_name("corpus.toml")
CORPUS_TODAY = "2026-10-01"
CORPUS_RECEIVED_AT = f"{CORPUS_TODAY}T09:00:00Z"
ROUTES = frozenset({"llm", "label", "reject"})
_MAIL_KEYS = {"name", "sender", "subject", "body", "routes", "needs_reply", "facts"}


@dataclass(frozen=True)
class LabCase:
    name: str
    email: EmailMessage
    accepts: Callable[[str], bool]
    # Yes/no questions whose answer is yes; None when nobody stated the truth for this mail.
    expected_signals: frozenset[str] | None = None
    # Invented mails say "tomorrow" relative to a fixed day, so that day is sent as today.
    today: str = ""


def load_corpus(path: Path = CORPUS_PATH) -> list[LabCase]:
    with open(path, "rb") as handle:
        mails = tomllib.load(handle).get("mail", [])
    cases = [_to_case(mail, path) for mail in mails]
    names = [case.name for case in cases]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ConfigurationError(f"{path}: duplicated mail name(s) {duplicated}")
    return cases


def cases_from_rated(cases: Iterable[Case], rules: RuleSet) -> tuple[list[LabCase], int]:
    """Rated mails as lab cases, and how many were left out because a rule decides them."""
    kept, decided_by_rule = [], 0
    for case in cases:
        # Production never asks JEV about these, so a question variant cannot change them.
        if rules.classify(case.email) is not None:
            decided_by_rule += 1
            continue
        kept.append(LabCase(_describe(case.email), case.email, case.satisfied_by))
    return kept, decided_by_rule


def _describe(email: EmailMessage) -> str:
    return f"{email.id} {extract_domain(email.sender)} {email.subject[:60]!r}"


def _to_case(mail: dict, path: Path) -> LabCase:
    where = f"{path}: mail {mail.get('name', '?')!r}"
    if set(mail) != _MAIL_KEYS:
        raise ConfigurationError(f"{where} must have exactly the keys {sorted(_MAIL_KEYS)}")
    routes = frozenset(mail["routes"])
    if not routes or not routes <= ROUTES:
        raise ConfigurationError(f"{where} has routes {sorted(routes)}; valid: {sorted(ROUTES)}")
    facts = frozenset(mail["facts"])
    if not facts <= set(SIGNAL_QUESTIONS) - {"needs_reply"}:
        raise ConfigurationError(f"{where} has unknown fact(s) {sorted(facts)}")
    email = EmailMessage(
        id=mail["name"],
        thread_id=mail["name"],
        sender=mail["sender"],
        subject=mail["subject"],
        snippet="",
        body=mail["body"],
        sender_domain=extract_domain(mail["sender"]),
        received_at=CORPUS_RECEIVED_AT,
    )
    expected = facts | ({"needs_reply"} if mail["needs_reply"] else frozenset())
    return LabCase(mail["name"], email, routes.__contains__, expected, CORPUS_TODAY)
