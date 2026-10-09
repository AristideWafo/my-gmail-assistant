import tomllib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from src.agent import profiles
from src.agent.loop import AgentLoop, Limits
from src.agent.profiles import AgentPorts
from src.agent.prompts import chat_system
from src.agent.tools import Toolbox
from src.domain import ANSWERED, AgentTurn, Trajectory, Usage
from src.errors import ConfigurationError
from src.evaluation.lab import percentile
from src.evaluation.runner import Tally
from src.evaluation.world import InMemoryMail, WorldMail
from src.ports import AgentModel, QuestionJudge
from src.storage import SqliteDecisionStore

SCENARIOS_PATH = Path(__file__).with_name("scenarios.toml")
# Scenarios say "yesterday" relative to a fixed day, so that day is the run's today.
SCENARIO_NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
_SCENARIO_KEYS = {
    "name", "profile", "prompt", "mail", "must_call", "must_not_call",
    "answer_contains", "answer_excludes",
}  # fmt: skip
_MAIL_KEYS = {"id", "thread", "sender", "subject", "body", "date", "to", "from_me"}
PROFILES: dict[str, Callable[[AgentPorts], Toolbox]] = {"chat_read": profiles.chat_read}


@dataclass(frozen=True)
class Scenario:
    name: str
    profile: str
    prompt: str
    mails: tuple[WorldMail, ...]
    must_call: frozenset[str] = frozenset()
    must_not_call: frozenset[str] = frozenset()
    answer_contains: tuple[str, ...] = ()
    answer_excludes: tuple[str, ...] = ()

    def failures(self, trajectory: Trajectory) -> list[str]:
        """Why the run is not what the scenario expects; empty when it is."""
        if trajectory.outcome != ANSWERED:
            return [f"stopped on {trajectory.outcome}"]
        called = {call.name for call in trajectory.tool_calls}
        answer = trajectory.answer.casefold()
        return [
            *(f"did not call {name}" for name in sorted(self.must_call - called)),
            *(f"called {name}" for name in sorted(self.must_not_call & called)),
            *(
                f"answer lacks {text!r}"
                for text in self.answer_contains
                if text.casefold() not in answer
            ),
            *(
                f"answer holds {text!r}"
                for text in self.answer_excludes
                if text.casefold() in answer
            ),
        ]


@dataclass
class ScenarioReport:
    name: str
    passed: Tally = field(default_factory=Tally)
    reasons: Counter = field(default_factory=Counter)
    steps: list[int] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    with open(path, "rb") as handle:
        entries = tomllib.load(handle).get("scenario", [])
    scenarios = [_to_scenario(entry, path) for entry in entries]
    names = [scenario.name for scenario in scenarios]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ConfigurationError(f"{path}: duplicated scenario name(s) {duplicated}")
    return scenarios


def run_scenario(
    scenario: Scenario,
    model: AgentModel,
    limits: Limits,
    repeats: int,
    judge: QuestionJudge | None = None,
    user_name: str = "",
    timer: Callable[[], float] = perf_counter,
) -> ScenarioReport:
    report = ScenarioReport(scenario.name)
    system = chat_system(user_name, SCENARIO_NOW.date())
    for _ in range(repeats):
        # A fresh world each time: a run must not find what the previous one left.
        store = SqliteDecisionStore(":memory:", clock=lambda: SCENARIO_NOW)
        try:
            ports = AgentPorts(
                InMemoryMail(list(scenario.mails)), store, judge, lambda: SCENARIO_NOW
            )
            toolbox = PROFILES[scenario.profile](ports)
            started = timer()
            trajectory = AgentLoop(model, limits).run(
                system, scenario.prompt, toolbox.specs, toolbox.execute
            )
            report.latencies.append(timer() - started)
        finally:
            store.close()
        failures = scenario.failures(trajectory)
        report.passed.add(not failures)
        report.reasons.update(failures)
        report.steps.append(sum(isinstance(message, AgentTurn) for message in trajectory.messages))
        report.usage += trajectory.usage
    return report


def below(reports: Sequence[ScenarioReport], min_pass: float) -> list[str]:
    return [report.name for report in reports if report.passed.rate < min_pass]


def format_trajectory_report(
    reports: Sequence[ScenarioReport], model_name: str, repeats: int, min_pass: float
) -> str:
    header = ["scenario", "passed", "steps", "p95 s", "tokens", "cost $", "most frequent failure"]
    rows = [
        [
            report.name,
            f"{report.passed.passed}/{report.passed.total}",
            f"{sum(report.steps) / len(report.steps):.1f}" if report.steps else "-",
            f"{percentile(report.latencies, 0.95):.1f}" if report.latencies else "-",
            str(report.usage.tokens),
            "-" if report.usage.cost_usd is None else f"{report.usage.cost_usd:.4f}",
            report.reasons.most_common(1)[0][0] if report.reasons else "",
        ]
        for report in reports
    ]
    table = [header, *rows]
    widths = [max(len(row[column]) for row in table) for column in range(len(header))]
    total = sum((report.usage for report in reports), Usage())
    failed = below(reports, min_pass)
    lines = [
        f"Model: {model_name}, {repeats} run(s) per scenario",
        "",
        *(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
            for row in table
        ),
        "",
        f"Total: {total.tokens} tokens"
        + ("" if total.cost_usd is None else f", ${total.cost_usd:.4f}"),
        f"Below {min_pass:.0%}: {', '.join(failed)}"
        if failed
        else f"All at {min_pass:.0%} or above.",
    ]
    return "\n".join(lines)


def _to_scenario(entry: dict, path: Path) -> Scenario:
    name = entry.get("name", "?")
    unknown = sorted(set(entry) - _SCENARIO_KEYS)
    if unknown:
        raise ConfigurationError(f"{path}: scenario {name!r} has unknown key(s) {unknown}")
    profile = entry.get("profile", "chat_read")
    if profile not in PROFILES:
        raise ConfigurationError(
            f"{path}: scenario {name!r} has unknown profile {profile!r}; known: {sorted(PROFILES)}"
        )
    try:
        return Scenario(
            name=entry["name"],
            profile=profile,
            prompt=entry["prompt"],
            mails=tuple(_to_mail(mail, name, path) for mail in entry.get("mail", [])),
            must_call=frozenset(entry.get("must_call", [])),
            must_not_call=frozenset(entry.get("must_not_call", [])),
            answer_contains=tuple(entry.get("answer_contains", [])),
            answer_excludes=tuple(entry.get("answer_excludes", [])),
        )
    except KeyError as missing:
        raise ConfigurationError(f"{path}: scenario {name!r} lacks {missing}") from None


def _to_mail(mail: dict, scenario: str, path: Path) -> WorldMail:
    unknown = sorted(set(mail) - _MAIL_KEYS)
    if unknown:
        raise ConfigurationError(f"{path}: a mail of {scenario!r} has unknown key(s) {unknown}")
    try:
        return WorldMail(
            id=mail["id"],
            thread_id=mail.get("thread", mail["id"]),
            sender=mail["sender"],
            subject=mail["subject"],
            body=mail["body"],
            date=mail["date"],
            to=tuple(mail.get("to", [])),
            from_me=mail.get("from_me", False),
        )
    except KeyError as missing:
        raise ConfigurationError(f"{path}: a mail of {scenario!r} lacks {missing}") from None
