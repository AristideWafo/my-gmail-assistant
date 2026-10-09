import logging

from src.agent import profiles
from src.agent.loop import AgentLoop, Limits
from src.agent.profiles import AgentPorts
from src.agent.prompts import followup_prompt, followup_system
from src.domain import WAITING_FOR_THEM, AgentRun, Trajectory, canonical_address
from src.followup.offers import recipients
from src.followup.state import OFFERED, SENT
from src.ports import AgentModel

logger = logging.getLogger(__name__)

STALE = "stale"


class FollowUpRuns:
    """Writes the follow-up of one sent mail left unanswered, reading that thread only. It
    offers and sends nothing: the text is kept on the tracked thread."""

    def __init__(
        self,
        model: AgentModel,
        ports: AgentPorts,
        limits: Limits,
        user_name: str = "",
        offers_use_it: bool = False,
    ) -> None:
        self._loop = AgentLoop(model, limits)
        self._ports = ports
        self._user_name = user_name
        # When offers carry the agent's text, a thread already offered went out with the fixed
        # one: writing now would show in /pending a text that is not the one [Envoyer] sends.
        self._offers_use_it = offers_use_it

    def run(self, run: AgentRun) -> Trajectory:
        thread_id, anchor_id = run.payload["thread_id"], run.payload["anchor_id"]
        thread = self._ports.store.threads.get(thread_id)
        if (
            thread is None
            or thread.state != WAITING_FOR_THEM
            or thread.anchor is None
            or thread.anchor.message_id != anchor_id
            or (self._offers_use_it and thread.proposal_state in (OFFERED, SENT))
        ):
            return Trajectory(STALE, ())
        to, cc = recipients(thread.anchor)
        notes = {
            address: [
                note.text for note in self._ports.store.memory.about(canonical_address(address))
            ]
            for address in to + cc
        }
        toolbox = profiles.followup_compose(self._ports, thread_id, anchor_id)
        return self._loop.run(
            followup_system(self._user_name, self._ports.clock().date()),
            followup_prompt(thread, notes),
            toolbox.specs,
            toolbox.execute,
        )

    def gave_up(self, run: AgentRun, reason: str) -> None:
        # Nobody asked: the offer goes out with the fixed text instead.
        logger.info("No follow-up written for thread %s (%s)", run.payload.get("thread_id"), reason)
