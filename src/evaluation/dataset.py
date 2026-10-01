from collections.abc import Callable, Iterable
from dataclasses import dataclass

from src.domain import Correction, EmailMessage, RatedDecision
from src.ports import MailProvider

# What a verdict says about the route the mail should have taken. A verdict rarely names the
# exact right answer, so each one is a constraint the replayed route must satisfy.
EXPECTATIONS: dict[str, Callable[[str, str], bool]] = {
    "valid": lambda route, recorded: route == recorded,
    "false_urgent": lambda route, recorded: route != "llm",
    "false_spam": lambda route, recorded: route == "reject",
    "missed_urgent": lambda route, recorded: route == "llm",
    "wrong_archive": lambda route, recorded: route != "reject",
    # Putting a mail forward is not a route: the verdict only rules out the archive.
    "missed_important": lambda route, recorded: route != "reject",
    # "Not worth putting forward" leaves both keeping and archiving open, not alerting.
    "false_important": lambda route, recorded: route != "llm",
}


@dataclass(frozen=True)
class Case:
    email: EmailMessage
    verdict: str
    recorded_route: str
    rated_at: str
    put_forward: bool = False

    def satisfied_by(self, route: str) -> bool:
        return EXPECTATIONS[self.verdict](route, self.recorded_route)


@dataclass(frozen=True)
class Dataset:
    cases: list[Case]
    # Corrections older than every case: the only ones a few-shot variant may learn from.
    training: list[Correction]
    missing: int
    split_at: str


def is_correction(rated: RatedDecision) -> bool:
    return rated.verdict != "valid"


def default_split(rated: list[RatedDecision]) -> str:
    """The moment that leaves half of the corrections before it, or "" when there are too few."""
    corrections = [item for item in rated if is_correction(item)]
    if len(corrections) < 2:
        return ""
    return corrections[len(corrections) // 2].rated_at


def build_dataset(
    rated: Iterable[RatedDecision], mail: MailProvider, split_at: str | None = None
) -> Dataset:
    rated = [item for item in rated if item.verdict in EXPECTATIONS]
    if split_at is None:
        split_at = default_split(rated)
    training, cases, missing = [], [], 0
    for item in rated:
        # A mail rated before the split may have been shown to the model as an example, so
        # scoring it would measure memory, not judgement: every variant skips it.
        if split_at and item.rated_at < split_at:
            if is_correction(item):
                training.append(_to_correction(item))
            continue
        email = mail.fetch_message(item.record.message_id)
        if email is None:
            missing += 1
            continue
        cases.append(
            Case(email, item.verdict, item.record.route, item.rated_at, item.record.put_forward)
        )
    return Dataset(cases=cases, training=training, missing=missing, split_at=split_at)


def _to_correction(rated: RatedDecision) -> Correction:
    record = rated.record
    return Correction(
        sender=record.sender,
        subject=record.subject,
        excerpt=record.excerpt,
        predicted_urgency=record.urgency,
        predicted_category=record.category,
        verdict=rated.verdict,
    )
