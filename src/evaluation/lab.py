import logging
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from src.evaluation.corpus import LabCase
from src.evaluation.runner import Tally
from src.evaluation.variants import Variant
from src.triage.engine import JevClassifier

logger = logging.getLogger(__name__)

SIGNAL_THRESHOLD = 0.5
LISTED_NAMES = 15


@dataclass
class SignalReport:
    yes: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    with_truth: int = 0


@dataclass
class LabReport:
    variant: str
    description: str = ""
    runs: list[Tally] = field(default_factory=list)
    misrouted: list[str] = field(default_factory=list)
    unstable: list[str] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    signals: dict[str, SignalReport] = field(default_factory=dict)
    forward: SignalReport | None = None


def planned_calls(variants: Sequence[Variant], cases: Sequence[LabCase], repeats: int) -> int:
    return len(variants) * len(cases) * repeats


def run_variant(
    variant: Variant,
    cases: Sequence[LabCase],
    classifier: JevClassifier,
    low_confidence_threshold: float,
    repeats: int = 1,
    clock: Callable[[], float] = time.perf_counter,
) -> LabReport:
    report = LabReport(
        variant.name,
        variant.description,
        signals={name: SignalReport() for name in variant.signals},
        forward=SignalReport() if variant.put_forward else None,
    )
    routes: dict[str, list[str | None]] = {case.name: [] for case in cases}
    for repeat in range(repeats):
        tally = Tally()
        for case in cases:
            answers = _ask(variant, case, classifier, report, clock)
            route = None
            if answers is not None:
                try:
                    route = variant.route(answers, low_confidence_threshold)
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("%s: unusable answer for %s: %r", variant.name, case.name, exc)
                    report.errors += 1
            # A failed call counts as a miss: in production that mail would not be judged.
            tally.add(route is not None and case.accepts(route))
            routes[case.name].append(route)
            if repeat == 0 and answers is not None:
                _record_signals(report, case, answers)
                _record_forward(report, variant, case, answers)
        report.runs.append(tally)
    for case in cases:
        seen = routes[case.name]
        if any(route is None or not case.accepts(route) for route in seen):
            report.misrouted.append(f"{case.name} -> {'/'.join(route or 'error' for route in seen)}")
        if len(set(seen)) > 1:
            report.unstable.append(case.name)
    return report


def _ask(
    variant: Variant,
    case: LabCase,
    classifier: JevClassifier,
    report: LabReport,
    clock: Callable[[], float],
) -> dict | None:
    request = variant.prepare(classifier.build_request(case.email))
    if case.today:
        request = {**request, "state": {**request["state"], "today": case.today}}
    started = clock()
    try:
        data = classifier.ask(request)
        answers = data["answers"]
    except Exception as exc:  # noqa: BLE001 - one failed call must not end the run
        logger.warning("%s failed on %s: %s", variant.name, case.name, type(exc).__name__)
        report.errors += 1
        return None
    report.latencies.append(clock() - started)
    usage = data.get("usage") or {}
    report.tokens.append(int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))
    return answers


def _record_signals(report: LabReport, case: LabCase, answers: dict) -> None:
    for name, signal in report.signals.items():
        probability = JevClassifier.parse_probability(answers.get(name))
        said_yes = probability is not None and probability >= SIGNAL_THRESHOLD
        expected = None if case.expected_signals is None else name in case.expected_signals
        _tally(signal, case.name, said_yes, expected)


def _record_forward(report: LabReport, variant: Variant, case: LabCase, answers: dict) -> None:
    if report.forward is None:
        return
    try:
        said_yes = variant.put_forward(answers, SIGNAL_THRESHOLD)
    except (KeyError, ValueError, TypeError):
        # Already counted as an unusable answer when the route was computed.
        return
    _tally(report.forward, case.name, said_yes, case.expected_forward)


def _tally(signal: SignalReport, name: str, said_yes: bool, expected: bool | None) -> None:
    if said_yes:
        signal.yes.append(name)
    if expected is None:
        return
    signal.with_truth += 1
    if expected and not said_yes:
        signal.missed.append(name)
    elif said_yes and not expected:
        signal.unexpected.append(name)


def format_lab_report(reports: Sequence[LabReport], case_count: int) -> str:
    lines = [f"Cases: {case_count}", ""]
    header = ["variant", "correct per run", "unstable", "tokens/mail", "median s", "p95 s", "errors"]
    rows = [
        [
            report.variant,
            " ".join(f"{run.passed}/{run.total}" for run in report.runs),
            str(len(report.unstable)),
            f"{statistics.mean(report.tokens):.0f}" if report.tokens else "-",
            f"{statistics.median(report.latencies):.2f}" if report.latencies else "-",
            f"{_percentile(report.latencies, 0.95):.2f}" if report.latencies else "-",
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
    for report in reports:
        lines += _details(report, case_count)
    return "\n".join(lines)


def _details(report: LabReport, cases: int) -> list[str]:
    lines = ["", f"[{report.variant}] {report.description}".rstrip()]
    lines.append(f"  misrouted ({len(report.misrouted)}): {_listed(report.misrouted)}")
    if report.unstable:
        lines.append(f"  route changed between runs: {_listed(report.unstable)}")
    if report.signals:
        lines.append(
            f"  yes/no questions, raw answers at {SIGNAL_THRESHOLD}, before any category or "
            "sender filter:"
        )
    for name, signal in report.signals.items():
        lines += _signal_lines(name, signal, cases)
    if report.forward is not None:
        lines.append(
            f"  put forward at {SIGNAL_THRESHOLD}, after the category filter (one of the "
            "questions above said yes):"
        )
        lines += _signal_lines("put forward", report.forward, cases)
    return lines


def _signal_lines(name: str, signal: SignalReport, cases: int) -> list[str]:
    lines = []
    if signal.with_truth:
        lines.append(
            f"  {name}: {len(signal.missed)} missed, {len(signal.unexpected)} unexpected "
            f"on {signal.with_truth} mails whose answer is known"
        )
        if signal.missed:
            lines.append(f"    missed: {_listed(signal.missed)}")
        if signal.unexpected:
            lines.append(f"    unexpected: {_listed(signal.unexpected)}")
    # Listed whenever some mails have no known answer: those can only be checked by hand.
    if signal.with_truth < cases:
        lines.append(f"  {name}: yes on {len(signal.yes)} of {cases} mails, to check by hand")
        if signal.yes:
            lines.append(f"    {_listed(signal.yes)}")
    return lines


def _listed(names: Sequence[str]) -> str:
    if not names:
        return "none"
    shown = "; ".join(names[:LISTED_NAMES])
    return shown if len(names) <= LISTED_NAMES else f"{shown}; and {len(names) - LISTED_NAMES} more"


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[round(fraction * (len(ordered) - 1))]
