import re
from dataclasses import dataclass

from src.triage.engine import TriageResult

_AUTOMATED_LOCAL_PART_RE = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|notification|newsletter|mailer-daemon|bounce|alerts?$)", re.IGNORECASE
)


@dataclass(frozen=True)
class SenderRule:
    pattern: re.Pattern[str]
    urgency: str
    category: str


def _rule(pattern: str, urgency: str, category: str) -> SenderRule:
    return SenderRule(re.compile(pattern, re.IGNORECASE), urgency, category)


# Bulk senders whose category is known: skipping the LLM call saves quota and removes classifier noise.
DEFAULT_RULES: tuple[SenderRule, ...] = (
    _rule(r"^(jobalerts-noreply|jobs-noreply|jobs-listings)@linkedin\.com$", "low", "alerte_emploi"),
    _rule(r"@([\w-]+\.)*substack\.com$", "low", "newsletter"),
    _rule(r"@news\.leboncoin\.fr$", "low", "promotion"),
)


def apply_rules(sender: str, rules: tuple[SenderRule, ...] = DEFAULT_RULES) -> TriageResult | None:
    for rule in rules:
        if rule.pattern.search(sender):
            return TriageResult(urgency=rule.urgency, category=rule.category, confidence=1.0)
    return None


def is_automated_sender(sender: str) -> bool:
    local_part = sender.split("@", 1)[0]
    return bool(_AUTOMATED_LOCAL_PART_RE.search(local_part))
