import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.domain import EmailMessage, TriageResult
from src.errors import ConfigurationError
from src.triage.taxonomy import CATEGORIES, URGENCIES

_AUTOMATED_LOCAL_PART_RE = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|ne[-_.]?pas[-_.]?repondre|notification|newsletter"
    r"|mailer-daemon|bounce|alerts?$)",
    re.IGNORECASE,
)
_RULE_KEYS = {"sender", "subject", "urgency", "category"}
_FILE_KEYS = {"vip", "rules"}
VIP_URGENCY = "high"
VIP_CATEGORY = "personnel"


@dataclass(frozen=True)
class SenderRule:
    pattern: re.Pattern[str]
    urgency: str
    category: str
    # None matches any subject.
    subject: re.Pattern[str] | None = None

    def matches(self, sender: str, subject: str) -> bool:
        if not self.pattern.search(sender):
            return False
        return self.subject is None or bool(self.subject.search(subject))


def _rule(pattern: str, urgency: str, category: str, subject: str | None = None) -> SenderRule:
    return SenderRule(
        re.compile(pattern, re.IGNORECASE),
        urgency,
        category,
        re.compile(subject, re.IGNORECASE) if subject else None,
    )


# Bulk senders whose category is known: skipping the LLM call saves quota and removes classifier noise.
DEFAULT_RULES: tuple[SenderRule, ...] = (
    _rule(r"^(jobalerts-noreply|jobs-noreply|jobs-listings)@linkedin\.com$", "low", "alerte_emploi"),
    _rule(r"@([\w-]+\.)*substack\.com$", "low", "newsletter"),
    _rule(r"@news\.leboncoin\.fr$", "low", "promotion"),
)


def apply_rules(
    sender: str, rules: tuple[SenderRule, ...] = DEFAULT_RULES, subject: str = ""
) -> TriageResult | None:
    for rule in rules:
        if rule.matches(sender, subject):
            return TriageResult(
                urgency=rule.urgency, category=rule.category, confidence=1.0, source="rule"
            )
    return None


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[SenderRule, ...] = DEFAULT_RULES
    vip: frozenset[str] = frozenset()

    def classify(self, email: EmailMessage) -> TriageResult | None:
        # From is trivially forged: a VIP only counts when the provider vouches for the address,
        # otherwise anyone could ring the phone by borrowing a VIP's name.
        if email.dmarc_pass and email.sender.lower() in self.vip:
            return TriageResult(VIP_URGENCY, VIP_CATEGORY, confidence=1.0, source="vip")
        return apply_rules(email.sender, self.rules, email.subject)


def load_ruleset(path: str) -> RuleSet:
    """Rules from the file come before the built-in ones; an empty path keeps the built-ins."""
    if not path:
        return RuleSet()
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(
            f"TRIAGE_RULES_PATH {path!r} cannot be read: {type(exc).__name__}"
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"TRIAGE_RULES_PATH {path!r} is not valid TOML: {exc}") from None
    _reject_unknown_keys(data, _FILE_KEYS, path)
    entries = _expect_list(data.get("rules", []), "rules", path)
    rules = tuple(_parse_rule(entry, index, path) for index, entry in enumerate(entries, start=1))
    return RuleSet(rules=rules + DEFAULT_RULES, vip=_parse_vip(data.get("vip", []), path))


def _parse_rule(entry: Any, index: int, path: str) -> SenderRule:
    where = f"{path!r} rule {index}"
    if not isinstance(entry, dict):
        raise ConfigurationError(f"{where} must be a table")
    _reject_unknown_keys(entry, _RULE_KEYS, where)
    for key in ("sender", "urgency", "category"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ConfigurationError(f"{where} needs a non-empty {key!r}")
    if entry["urgency"] not in URGENCIES:
        raise ConfigurationError(f"{where} has urgency {entry['urgency']!r}; valid: {sorted(URGENCIES)}")
    if entry["category"] not in CATEGORIES:
        raise ConfigurationError(
            f"{where} has category {entry['category']!r}; valid: {sorted(CATEGORIES)}"
        )
    subject = entry.get("subject")
    if subject is not None and (not isinstance(subject, str) or not subject):
        raise ConfigurationError(f"{where} has an empty or non-text 'subject'")
    try:
        return _rule(entry["sender"], entry["urgency"], entry["category"], subject)
    except re.error as exc:
        raise ConfigurationError(f"{where} has an invalid pattern: {exc}") from None


def _parse_vip(entries: Any, path: str) -> frozenset[str]:
    addresses = _expect_list(entries, "vip", path)
    for address in addresses:
        # Exact addresses only: a pattern here would turn the VIP list into a second rule engine.
        if not isinstance(address, str) or address.count("@") != 1 or " " in address:
            raise ConfigurationError(f"{path!r} vip entry {address!r} is not an email address")
    return frozenset(address.lower() for address in addresses)


def _expect_list(value: Any, key: str, path: str) -> list:
    if not isinstance(value, list):
        raise ConfigurationError(f"{path!r} key {key!r} must be a list")
    return value


def _reject_unknown_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigurationError(f"{where} has unknown key(s) {unknown}; valid: {sorted(allowed)}")


def is_automated_sender(sender: str) -> bool:
    local_part = sender.split("@", 1)[0]
    return bool(_AUTOMATED_LOCAL_PART_RE.search(local_part))
