"""What a run may do, decided by what started it and by nothing it reads afterwards.

A run started by the user's own message may search the whole mailbox. A run started by a mail
or a thread reads that thread only: whatever its text says, there is no tool to reach further.
No profile holds a tool that writes to the mailbox."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from src.agent.scope import WHOLE_MAILBOX, MailScope, only_thread
from src.agent.tools import Tool, Toolbox
from src.agent.toolsets.judgment import judgment_tools
from src.agent.toolsets.mail import mail_tools
from src.agent.toolsets.tracking import tracking_tools
from src.ports import DecisionStore, MailProvider, QuestionJudge


@dataclass(frozen=True)
class AgentPorts:
    mail: MailProvider
    store: DecisionStore
    judge: QuestionJudge | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def chat_read(ports: AgentPorts) -> Toolbox:
    return Toolbox([*_reading(ports, WHOLE_MAILBOX), *tracking_tools(ports.store)])


def thread_bound(ports: AgentPorts, thread_id: str) -> Toolbox:
    return Toolbox(_reading(ports, only_thread(thread_id)))


def _reading(ports: AgentPorts, scope: MailScope) -> list[Tool]:
    return [
        *mail_tools(ports.mail, scope),
        *judgment_tools(ports.judge, ports.mail, scope, ports.clock),
    ]
