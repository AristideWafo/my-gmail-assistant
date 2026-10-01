from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import timedelta

from src.domain import CommandEvent, FeedbackTally
from src.interactions.review import ROUTE_LABELS
from src.ports import ChatInbox, DecisionStore

VOLUME_WINDOW = timedelta(days=30)
NO_FEEDBACK = "Aucun verdict pour l'instant : utilise les boutons des alertes ou /review."
UNKNOWN_SOURCE = "inconnue"
_ROUTE_ORDER = ("llm", "label", "reject")
_MISTAKE_LABELS = {
    "false_urgent": "faux urgent",
    "false_spam": "spam",
    "missed_urgent": "urgent raté",
    "wrong_archive": "à garder",
    "missed_important": "à mettre en avant",
}


def format_stats(tallies: Iterable[FeedbackTally], volume_by_source: dict[str, int]) -> str:
    tallies = list(tallies)
    sections = []
    if tallies:
        total = sum(tally.count for tally in tallies)
        sections.append(f"📊 Précision du tri — verdicts : {total}")
        sections.append(_section("Par décision", tallies, lambda t: t.route, _route_name))
        sections.append(_section("Par source", tallies, lambda t: t.source, _source_name))
    else:
        sections.append(NO_FEEDBACK)
    if volume_by_source:
        volume = " · ".join(
            f"{_source_name(source)} {count}" for source, count in volume_by_source.items()
        )
        sections.append(f"Décisions sur {VOLUME_WINDOW.days} jours\n{volume}")
    return "\n\n".join(sections)


def _section(
    title: str,
    tallies: list[FeedbackTally],
    key: Callable[[FeedbackTally], str],
    name: Callable[[str], str],
) -> str:
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tally in tallies:
        groups[key(tally)][tally.verdict] += tally.count
    lines = [f"• {name(group)} : {_summary(groups[group])}" for group in _ordered(groups)]
    return "\n".join([title, *lines])


def _ordered(groups: dict[str, dict[str, int]]) -> list[str]:
    known = [route for route in _ROUTE_ORDER if route in groups]
    return known + sorted(group for group in groups if group not in _ROUTE_ORDER)


def _summary(verdicts: dict[str, int]) -> str:
    rated = sum(verdicts.values())
    parts = [f"{verdicts.get('valid', 0) / rated:.0%} corrects sur {rated}"]
    parts += [
        f"{label} ×{verdicts[verdict]}"
        for verdict, label in _MISTAKE_LABELS.items()
        if verdicts.get(verdict)
    ]
    return " · ".join(parts)


def _route_name(route: str) -> str:
    return ROUTE_LABELS.get(route, route)


def _source_name(source: str) -> str:
    return source or UNKNOWN_SOURCE


class StatsCommand:
    def __init__(self, store: DecisionStore, chat: ChatInbox) -> None:
        self._store = store
        self._chat = chat

    def run(self, event: CommandEvent) -> None:
        text = format_stats(
            self._store.feedback_breakdown(),
            self._store.decision_counts_by_source(VOLUME_WINDOW),
        )
        self._chat.send_message(text, reply_to=event.message_id)
