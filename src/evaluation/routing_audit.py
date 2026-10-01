from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from src.domain import DecisionRecord, RatedDecision
from src.evaluation.corpus import SUBJECT_CHARS
from src.evaluation.dataset import EXPECTATIONS
from src.gmail.text_cleaning import extract_domain

# Decided without the classifier: no routing rule on its answers applies to them.
DETERMINISTIC_SOURCES = {"rule", "vip"}
LISTED = 10


@dataclass(frozen=True)
class RoutingRule:
    name: str
    description: str
    # The decision carries the urgency, category and confidence the classifier gave.
    matches: Callable[[DecisionRecord, float], bool]
    existing: bool = False
    route: str = "reject"


def _confident(record: DecisionRecord, category: str, urgency: str, threshold: float) -> bool:
    return (
        record.category == category
        and record.urgency == urgency
        and record.confidence >= threshold
    )


RULES = (
    RoutingRule(
        "archive-low-notification",
        "notification_systeme, low urgency, confident -> archive (in production today)",
        lambda record, threshold: _confident(record, "notification_systeme", "low", threshold),
        existing=True,
    ),
    RoutingRule(
        "archive-low-alerte-technique",
        "alerte_technique, low urgency, confident -> archive (CI success, dependency bumps)",
        lambda record, threshold: _confident(record, "alerte_technique", "low", threshold),
    ),
    RoutingRule(
        "archive-urgent-spam",
        "spam claiming to be urgent, confident -> archive (kept in the inbox today)",
        lambda record, threshold: _confident(record, "spam", "high", threshold),
    ),
)


@dataclass
class RuleAudit:
    rule: RoutingRule
    matched: int = 0
    # Mails whose stored route is not the rule's: what adopting it would change.
    moved: int = 0
    agree: int = 0
    disagree: list[str] = field(default_factory=list)


def audit(
    rules: Iterable[RoutingRule],
    records: Iterable[DecisionRecord],
    rated: Iterable[RatedDecision],
    threshold: float,
) -> list[RuleAudit]:
    """Replays each rule on stored answers and confronts it with the verdicts; no call is made."""
    verdicts = {item.record.message_id: item.verdict for item in rated}
    audits = [RuleAudit(rule) for rule in rules]
    for record in records:
        if record.source in DETERMINISTIC_SOURCES:
            continue
        for result in audits:
            if not result.rule.matches(record, threshold):
                continue
            result.matched += 1
            result.moved += int(record.route != result.rule.route)
            verdict = verdicts.get(record.message_id)
            if verdict is None or verdict not in EXPECTATIONS:
                continue
            if EXPECTATIONS[verdict](result.rule.route, record.route):
                result.agree += 1
            else:
                result.disagree.append(_describe(record, verdict))
    return audits


def format_audit(audits: Iterable[RuleAudit], decisions: int, days: int) -> str:
    lines = [f"Classifier decisions over {days} days: {decisions}", ""]
    for result in audits:
        rule = result.rule
        kind = "existing" if rule.existing else "candidate"
        lines.append(f"[{kind}] {rule.name}: {rule.description}")
        effect = (
            f"applies to {result.matched} mail(s)"
            if rule.existing
            else f"would move {result.moved} of {result.matched} matching mail(s)"
        )
        rated = result.agree + len(result.disagree)
        lines.append(
            f"  {effect}; {rated} rated: {result.agree} verdict(s) agree, "
            f"{len(result.disagree)} disagree"
        )
        lines += [f"    disagrees: {name}" for name in result.disagree[:LISTED]]
        if len(result.disagree) > LISTED:
            lines.append(f"    and {len(result.disagree) - LISTED} more")
        lines.append("")
    return "\n".join(lines).rstrip()


def _describe(record: DecisionRecord, verdict: str) -> str:
    subject = " ".join(record.subject.split())[:SUBJECT_CHARS]
    return (
        f"{record.message_id} {extract_domain(record.sender)} «{subject}» "
        f"[{verdict}, was {record.route}]"
    )
