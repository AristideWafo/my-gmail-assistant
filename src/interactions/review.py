import logging
import random
from collections.abc import Iterable
from datetime import timedelta

from src.domain import CommandEvent, DecisionRecord
from src.formatting import truncate
from src.interactions.callbacks import review_buttons
from src.ports import ChatInbox, DecisionStore

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE = 5
MAX_SAMPLE = 10
REVIEW_WINDOW = timedelta(days=7)
CANDIDATE_POOL = 200
EXCERPT_CHARS = 200
USAGE = f"Usage : /review [n], avec n entre 1 et {MAX_SAMPLE}."
NOTHING_TO_REVIEW = "Rien à vérifier : aucun mail non noté sur les 7 derniers jours."
ROUTE_LABELS = {"llm": "alerté", "label": "étiqueté", "reject": "archivé"}


def parse_sample_size(args: str) -> int | None:
    if not args:
        return DEFAULT_SAMPLE
    try:
        size = int(args)
    except ValueError:
        return None
    return size if 1 <= size <= MAX_SAMPLE else None


def select_sample(
    candidates: Iterable[DecisionRecord], size: int, rng: random.Random
) -> list[DecisionRecord]:
    """Half archived mails, half labeled ones closest to a doubtful call, one per sender."""
    archived, labeled = [], []
    for record in candidates:
        (archived if record.route == "reject" else labeled).append(record)
    # Archived mail is the costly blind spot (nobody ever sees it again), but it is mostly
    # newsletters: shuffled so a few loud senders do not fill every review.
    rng.shuffle(archived)
    labeled.sort(key=lambda record: record.confidence)

    sample: list[DecisionRecord] = []
    seen: set[str] = set()

    def take(pool: list[DecisionRecord], count: int) -> None:
        for record in pool:
            if count <= 0:
                return
            if record.sender in seen:
                continue
            seen.add(record.sender)
            sample.append(record)
            count -= 1

    take(archived, (size + 1) // 2)
    take(labeled, size - len(sample))
    take(archived, size - len(sample))
    return sample


def format_review(record: DecisionRecord, position: int, total: int) -> str:
    outcome = ROUTE_LABELS.get(record.route, record.route)
    classification = " · ".join(
        [record.urgency, record.category, outcome, f"confiance {record.confidence:.0%}"]
    )
    lines = [
        f"📋 À vérifier {position}/{total}",
        f"De : {record.sender}",
        f"Objet : {record.subject}",
        f"Classé : {classification}",
    ]
    excerpt = truncate(" ".join(record.excerpt.split()), EXCERPT_CHARS)
    return "\n".join(lines) + (f"\n\n{excerpt}" if excerpt else "")


class ReviewCommand:
    def __init__(
        self, store: DecisionStore, chat: ChatInbox, rng: random.Random | None = None
    ) -> None:
        self._store = store
        self._chat = chat
        self._rng = rng or random.Random()

    def run(self, event: CommandEvent) -> None:
        size = parse_sample_size(event.args)
        if size is None:
            self._chat.send_message(USAGE, reply_to=event.message_id)
            return
        candidates = self._store.review_candidates(REVIEW_WINDOW, CANDIDATE_POOL)
        reviewable = [
            (record, buttons)
            for record in select_sample(candidates, size, self._rng)
            if (buttons := review_buttons(record.message_id, record.route)) is not None
        ]
        if not reviewable:
            self._chat.send_message(NOTHING_TO_REVIEW, reply_to=event.message_id)
            return
        for position, (record, buttons) in enumerate(reviewable, start=1):
            self._chat.send_message(
                format_review(record, position, len(reviewable)), buttons=buttons
            )
