import json
import math
import os
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, tzinfo

from src.domain import ThreadSnapshot, canonical_address
from src.followup.state import add_business_days
from src.ports import MailProvider, SentMailJudge
from src.triage.rules import is_automated_sender

DEFAULT_SAMPLES_PATH = "data/sent_samples.json"
TO_LABEL = 60
THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
TEXT_SHOWN = 800


@dataclass
class SentSample:
    message_id: str
    thread_id: str
    sent_at: str
    subject: str
    text: str
    recipients: int
    probability: float | None = None
    # Whether a person wrote in the thread within the follow-up delay; None while too recent.
    answered_in_time: bool | None = None
    to_label: bool = False
    label: bool | None = None


def sample_messages(
    snapshot: ThreadSnapshot,
    my_addresses: frozenset[str],
    since: datetime,
    after_days: int,
    timezone: tzinfo,
    now: datetime,
) -> list[SentSample]:
    """Every mail I wrote in the thread that the tracker could have followed, without its text."""
    samples = []
    messages = snapshot.messages
    for position, message in enumerate(messages):
        if not message.from_me or message.automated or message.sent_at < since:
            continue
        recipients = [
            address
            for address in message.to + message.cc
            if canonical_address(address) not in my_addresses
        ]
        if not recipients or all(is_automated_sender(address) for address in recipients):
            continue
        due = add_business_days(message.sent_at, after_days, timezone)
        answered = None
        if due <= now:
            answered = any(
                not later.from_me and not later.automated and later.sent_at <= due
                for later in messages[position + 1 :]
            )
        samples.append(
            SentSample(
                message_id=message.id,
                thread_id=snapshot.thread_id,
                sent_at=message.sent_at.isoformat(),
                subject=message.subject,
                text="",
                recipients=len(recipients),
                answered_in_time=answered,
            )
        )
    return samples


def find_samples(
    mail: MailProvider,
    days: int,
    max_threads: int,
    after_days: int,
    timezone: tzinfo,
    now: datetime,
) -> list[SentSample]:
    mine = mail.my_addresses()
    since = now - timedelta(days=days)
    samples = []
    for ref in mail.sent_threads(days, max_threads):
        snapshot = mail.thread_snapshot(ref.thread_id)
        if snapshot is not None:
            samples.extend(sample_messages(snapshot, mine, since, after_days, timezone, now))
    return samples


def judge_samples(
    samples: Sequence[SentSample], mail: MailProvider, judge: SentMailJudge
) -> int:
    """Fills text and probability; returns how many could not be judged."""
    failed = 0
    for sample in samples:
        try:
            sample.text = mail.sent_text(sample.message_id)
            sample.probability = judge.expects_answer(sample.subject, sample.text)
        except Exception:  # noqa: BLE001 - one failure must not lose the rest of the run
            failed += 1
    return failed


def pick_for_labelling(samples: Sequence[SentSample], count: int = TO_LABEL, seed: int = 0) -> None:
    """Spreads the picks over the whole probability range, half of them below 0.5: labelling
    only high scorers would make precision look better than it is."""
    judged = [s for s in samples if s.probability is not None]
    rng = random.Random(seed)
    low = [s for s in judged if s.probability < 0.5]
    high = [s for s in judged if s.probability >= 0.5]
    picked = _spread(low, count // 2, rng) + _spread(high, count - count // 2, rng)
    for sample in picked:
        sample.to_label = True


def _spread(samples: list[SentSample], count: int, rng: random.Random) -> list[SentSample]:
    ordered = sorted(samples, key=lambda s: s.probability)
    if len(ordered) <= count:
        return ordered
    step = len(ordered) / count
    return [ordered[min(int(i * step + rng.random() * step), len(ordered) - 1)] for i in range(count)]


def label_samples(
    samples: Sequence[SentSample],
    ask: Callable[[str], str],
    show: Callable[[str], None],
    save: Callable[[], None],
) -> int:
    """Asks yes / no for each picked mail not labelled yet; returns how many were labelled."""
    pending = [s for s in samples if s.to_label and s.label is None]
    done = 0
    for position, sample in enumerate(pending, start=1):
        show(
            f"\n[{position}/{len(pending)}] {sample.subject} "
            f"({sample.recipients} recipient(s), {sample.sent_at[:10]})\n"
            f"{sample.text[:TEXT_SHOWN] or '(no text)'}"
        )
        answer = ""
        while answer not in ("y", "n", "s", "q"):
            answer = ask("Did this mail wait for an answer? [y]es / [n]o / [s]kip / [q]uit: ")
            answer = answer.strip().lower()[:1]
        if answer == "q":
            break
        if answer == "s":
            continue
        sample.label = answer == "y"
        done += 1
        save()
    return done


def format_sent_report(samples: Sequence[SentSample]) -> str:
    judged = [s for s in samples if s.probability is not None]
    labelled = [s for s in judged if s.label is not None]
    timed = [s for s in judged if s.answered_in_time is not None]
    lines = [
        (
            f"{len(judged)} sent mail(s) judged, {len(labelled)} labelled, "
            f"{len(timed)} old enough to know whether an answer came in time."
        ),
        "",
        "Against your labels (yes = waited for an answer):",
        "threshold  precision        recall           offered",
    ]
    for threshold in THRESHOLDS:
        said_yes = [s for s in labelled if s.probability >= threshold]
        true_yes = [s for s in labelled if s.label]
        hits = sum(1 for s in said_yes if s.label)
        lines.append(
            f"{threshold:>9.1f}  {_rate(hits, len(said_yes)):<15}  "
            f"{_rate(hits, len(true_yes)):<15}  {len(said_yes)}"
        )
    lines += [
        "",
        (
            "Answer in the thread within the delay, by JEV's answer (a hint only: people "
            "answer unasked, and ignore real requests):"
        ),
        "threshold  answered if yes  answered if no",
    ]
    for threshold in THRESHOLDS:
        yes = [s for s in timed if s.probability >= threshold]
        no = [s for s in timed if s.probability < threshold]
        lines.append(
            f"{threshold:>9.1f}  {_rate(sum(s.answered_in_time for s in yes), len(yes)):<15}  "
            f"{_rate(sum(s.answered_in_time for s in no), len(no))}"
        )
    return "\n".join(lines)


def _rate(hits: int, total: int) -> str:
    if not total:
        return "n/a"
    return f"{hits / total:.2f} ±{_wilson_half_width(hits, total):.2f}"


def _wilson_half_width(hits: int, total: int, z: float = 1.96) -> float:
    p = hits / total
    denominator = 1 + z * z / total
    return z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator


def save_samples(path: str, samples: Sequence[SentSample]) -> None:
    # Personal mail: readable by its owner only, and kept under data/, which git ignores.
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump([asdict(s) for s in samples], handle, ensure_ascii=False, indent=1)


def load_samples(path: str) -> list[SentSample]:
    with open(path, encoding="utf-8") as handle:
        return [SentSample(**raw) for raw in json.load(handle)]
