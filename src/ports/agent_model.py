from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from src.domain import AgentMessage, AgentTurn, ToolSpec


@runtime_checkable
class AgentModel(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def check_connection(self) -> str: ...

    def step(
        self, system: str, messages: Sequence[AgentMessage], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        """One turn of the model over the whole conversation so far: text, tool calls to run,
        or both. Runs no tool itself. Raises AgentModelError when no turn could be obtained."""
        ...
