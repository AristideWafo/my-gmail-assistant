"""What a run may do, decided by what started it and by nothing it reads afterwards.

A run started by the user's own message may search the whole mailbox. A run started by a mail
or a thread reads that thread only: whatever its text says, there is no tool to reach further.
No profile holds a tool that writes to the mailbox: the most a run can do is show the user
a reply for them to send."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from src.agent.scope import WHOLE_MAILBOX, MailScope, only_thread
from src.agent.tools import Tool, Toolbox
from src.agent.toolsets.judgment import judgment_tools
from src.agent.toolsets.mail import mail_tools
from src.agent.toolsets.memory import memory_tools
from src.agent.toolsets.proposals import Revised, proposal_tools
from src.agent.toolsets.tracking import tracking_tools
from src.domain import Button
from src.ports import ChatInbox, DecisionStore, MailProvider, QuestionJudge


@dataclass(frozen=True)
class AgentPorts:
    mail: MailProvider
    store: DecisionStore
    judge: QuestionJudge | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def chat_read(ports: AgentPorts, said: str | None = None) -> Toolbox:
    """`said`, the user's own message, gives the run a memory: it may keep passages of it."""
    return Toolbox(_chat_reading(ports, said))


def _chat_reading(ports: AgentPorts, said: str | None) -> list[Tool]:
    return [
        *_reading(ports, WHOLE_MAILBOX),
        *tracking_tools(ports.store),
        *(memory_tools(ports.store.memory, said) if said is not None else ()),
    ]


def chat_propose(
    ports: AgentPorts,
    chat: ChatInbox,
    buttons: Callable[[str], list[list[Button]] | None],
    reply_to: int | None,
    revised: Revised | None = None,
    said: str | None = None,
) -> Toolbox:
    """`chat_read`, plus showing the user a reply to send or not. Only for a run the user
    started: a proposal is their request, never a mail's."""
    return Toolbox(
        [
            *_chat_reading(ports, said),
            *proposal_tools(ports.mail, ports.store, chat, buttons, reply_to, revised),
        ]
    )


def thread_bound(ports: AgentPorts, thread_id: str) -> Toolbox:
    return Toolbox(_reading(ports, only_thread(thread_id)))


def _reading(ports: AgentPorts, scope: MailScope) -> list[Tool]:
    return [
        *mail_tools(ports.mail, scope),
        *judgment_tools(ports.judge, ports.mail, scope, ports.clock),
    ]
