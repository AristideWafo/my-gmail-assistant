import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from src.evaluation.dataset import Case
from src.ports import EmailClassifier
from src.triage.rules import apply_rules
from src.workflow import route_for

logger = logging.getLogger(__name__)

MIN_VERDICTS = 100
MIN_CORRECTIONS = 20


@dataclass
class Tally:
    passed: int = 0
    total: int = 0

    def add(self, passed: bool) -> None:
        self.total += 1
        self.passed += int(passed)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass
class VariantReport:
    name: str
    overall: Tally = field(default_factory=Tally)
    by_verdict: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    classifier_calls: int = 0
    errors: int = 0


def evaluate(
    name: str, classifier: EmailClassifier, cases: Iterable[Case], low_confidence_threshold: float
) -> VariantReport:
    """Replays each case through the rules and `classifier`, exactly as the workflow routes it."""
    report = VariantReport(name)
    for case in cases:
        triage = apply_rules(case.email.sender)
        if triage is None:
            report.classifier_calls += 1
            try:
                triage = classifier.classify(case.email)
            except Exception as exc:  # noqa: BLE001 - one failed call must not end the run
                logger.warning("%s failed on %s: %s", name, case.email.id, type(exc).__name__)
                report.errors += 1
        # A classifier error counts as a miss: in production that mail would not be judged.
        passed = triage is not None and case.satisfied_by(
            route_for(triage, low_confidence_threshold)
        )
        report.overall.add(passed)
        report.by_verdict[case.verdict].add(passed)
    return report


def is_conclusive(cases: list[Case]) -> bool:
    corrections = sum(1 for case in cases if case.verdict != "valid")
    return len(cases) >= MIN_VERDICTS and corrections >= MIN_CORRECTIONS


def format_report(
    reports: list[VariantReport], cases: list[Case], missing: int, split_at: str
) -> str:
    corrections = sum(1 for case in cases if case.verdict != "valid")
    lines = [
        f"Cases: {len(cases)} ({corrections} corrections), {missing} mail(s) no longer available",
        f"Split: {split_at or 'none (too few corrections, few-shot not evaluated)'}",
        "",
    ]
    verdicts = sorted({case.verdict for case in cases})
    header = ["variant", "overall", *verdicts, "calls", "errors"]
    rows = [
        [
            report.name,
            _cell(report.overall),
            *(_cell(report.by_verdict[verdict]) for verdict in verdicts),
            str(report.classifier_calls),
            str(report.errors),
        ]
        for report in reports
    ]
    table = [header, *rows]
    widths = [max(len(row[column]) for row in table) for column in range(len(header))]
    lines += [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in table
    ]
    if not is_conclusive(cases):
        warning = (
            f"NOT CONCLUSIVE: fewer than {MIN_VERDICTS} cases including {MIN_CORRECTIONS} "
            "corrections; differences between variants may be chance."
        )
        lines += ["", warning]
    return "\n".join(lines)


def _cell(tally: Tally) -> str:
    return f"{tally.rate:.0%} ({tally.passed}/{tally.total})" if tally.total else "-"
