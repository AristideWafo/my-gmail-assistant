from dataclasses import dataclass

from src.agent.schema import arguments, text
from src.agent.tools import Tool, ToolRefused
from src.domain import ToolSpec

# What the model says, and the route of the triage it stands for.
ROUTES = {"alert": "llm", "keep": "label", "archive": "reject"}
# From the least to the most visible to the user.
VISIBILITY = ("reject", "label", "llm")
UNKNOWN_ROUTE = f"`route` must be one of: {', '.join(ROUTES)}."
DECIDED = "Noted."


@dataclass
class Decision:
    """Where a replayed run said the mail should go; empty until it says so."""

    route: str = ""
    reason: str = ""


def more_visible(route: str, floor: str) -> str:
    """The agent's route, unless it would make the mail less visible than the deterministic one:
    it may rescue a mail, never bury one."""
    return route if VISIBILITY.index(route) >= VISIBILITY.index(floor) else floor


def triage_tools(decision: Decision) -> list[Tool]:
    """`decide` only records an opinion: a replay touches no mail."""

    def decide(args: dict) -> str:
        if args["route"] not in ROUTES:
            raise ToolRefused(UNKNOWN_ROUTE)
        decision.route = ROUTES[args["route"]]
        decision.reason = " ".join(args["reason"].split())
        return DECIDED

    return [
        Tool(
            ToolSpec(
                "decide",
                "Says what should be done with the mail: alert (notify the user now), keep "
                "(leave it in the inbox, labelled) or archive (out of the inbox, unread). "
                "Call it once; it ends your turn.",
                arguments(
                    route=text("alert, keep or archive.", 10),
                    reason=text("Why, in one sentence.", 300),
                ),
            ),
            decide,
            ends_run=True,
        )
    ]
