import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from src.agent import profiles
from src.agent.loop import AgentLoop, Limits
from src.agent.profiles import AgentPorts
from src.agent.prompts import triage_prompt, triage_system
from src.agent.toolsets.triage import Decision, more_visible
from src.domain import TriageResult, Usage
from src.evaluation.dataset import Case
from src.evaluation.runner import Tally
from src.ports import AgentModel, EmailClassifier
from src.workflow import route_for

logger = logging.getLogger(__name__)

# Under it, JEV is not sure of itself: where a second opinion has the most room to help.
UNSURE_BELOW = 0.7
WITH_JEV = "agent, shown JEV's verdict"
ALONE = "agent, not shown it"
DETERMINISTIC = "deterministic (today)"
SLICES = ("all", "JEV unsure", "corrected by you")
NOT_CONCLUSIVE = (
    "Few mails in a slice prove nothing: read the counts, not the percentages. An arm shown "
    "JEV's verdict tends to repeat it, so its agreement with today's route is not a result."
)


@dataclass
class ArmReport:
    name: str
    by_slice: dict[str, Tally] = field(default_factory=lambda: {name: Tally() for name in SLICES})
    undecided: int = 0
    differs: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class Judged:
    """A rated mail with what JEV says of it now and the route the code derives from that."""

    case: Case
    verdict: TriageResult
    route: str

    @property
    def slices(self) -> list[str]:
        unsure = self.verdict.confidence < UNSURE_BELOW
        corrected = self.case.verdict != "valid"
        return [
            "all",
            *(["JEV unsure"] if unsure else []),
            *(["corrected by you"] if corrected else []),
        ]


def judge(cases: Sequence[Case], classifier: EmailClassifier, threshold: float) -> list[Judged]:
    judged = []
    for case in cases:
        try:
            verdict = classifier.classify(case.email)
        except Exception as exc:  # noqa: BLE001 - one failed call must not end the replay
            logger.warning("JEV failed on %s: %s", case.email.id, type(exc).__name__)
            continue
        judged.append(Judged(case, verdict, route_for(verdict, threshold)))
    return judged


def replay(
    judged: Sequence[Judged],
    model: AgentModel,
    ports: AgentPorts,
    limits: Limits,
    user_name: str = "",
    clock: Callable[[], datetime] | None = None,
) -> list[ArmReport]:
    """Each mail goes through the agent twice, shown JEV's verdict and not, and each answer is
    scored as given and as it would count in production, where the agent can only make a mail
    more visible than the deterministic route."""
    today = (clock or ports.clock)().date()
    system = triage_system(user_name, today)
    deterministic = ArmReport(DETERMINISTIC)
    arms = {
        shown: (ArmReport(name), ArmReport(f"{name}, never less visible"))
        for shown, name in ((True, WITH_JEV), (False, ALONE))
    }
    for one in judged:
        _score(deterministic, one, one.route)
        for shown, (as_given, floored) in arms.items():
            decision = Decision()
            toolbox = profiles.triage_replay(ports, one.case.email.thread_id, decision)
            trajectory = AgentLoop(model, limits).run(
                system,
                triage_prompt(one.case.email, one.verdict if shown else None),
                toolbox.specs,
                toolbox.execute,
            )
            as_given.usage += trajectory.usage
            if not decision.route:
                # No opinion counts as a miss as given; in production the route stands.
                as_given.undecided += 1
                _score(as_given, one, None)
                _score(floored, one, one.route)
                continue
            _score(as_given, one, decision.route)
            _score(floored, one, more_visible(decision.route, one.route))
    return [deterministic, *(report for pair in arms.values() for report in pair)]


def format_replay_report(reports: Sequence[ArmReport], judged: int, model_name: str) -> str:
    header = ["arm", *SLICES, "differs from today", "no opinion", "tokens", "cost $"]
    rows = [
        [
            report.name,
            *(_cell(report.by_slice[name]) for name in SLICES),
            str(report.differs),
            str(report.undecided),
            str(report.usage.tokens),
            "-" if report.usage.cost_usd is None else f"{report.usage.cost_usd:.4f}",
        ]
        for report in reports
    ]
    table = [header, *rows]
    widths = [max(len(row[column]) for row in table) for column in range(len(header))]
    return "\n".join(
        [
            f"Model: {model_name}, {judged} rated mail(s) not decided by a rule",
            "A cell is the mails whose route agrees with your verdict.",
            "",
            *(
                "  ".join(
                    cell.ljust(width) for cell, width in zip(row, widths, strict=True)
                ).rstrip()
                for row in table
            ),
            "",
            NOT_CONCLUSIVE,
        ]
    )


def _score(report: ArmReport, one: Judged, route: str | None) -> None:
    passed = route is not None and one.case.satisfied_by(route)
    for name in one.slices:
        report.by_slice[name].add(passed)
    if route is not None and route != one.route:
        report.differs += 1


def _cell(tally: Tally) -> str:
    return f"{tally.rate:.0%} ({tally.passed}/{tally.total})" if tally.total else "-"
