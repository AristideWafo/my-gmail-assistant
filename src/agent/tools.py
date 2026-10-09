from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.agent.schema import problem_with
from src.domain import ToolCall, ToolResult, ToolSpec

UNKNOWN_TOOL = "Unknown tool."


class ToolRefused(Exception):
    """The tool will not do what was asked; the message is what the model is told."""


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    run: Callable[[dict], str]
    # Running it is what the run was for: the model gets no turn after it.
    ends_run: bool = False


class Toolbox:
    """The tools one run may call, and nothing else: a name outside it does not exist."""

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.spec.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("two tools share a name")

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self._tools.values())

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name) if isinstance(call.name, str) else None
        if tool is None:
            return ToolResult(call, UNKNOWN_TOOL, is_error=True)
        problem = problem_with(tool.spec.parameters, call.args)
        if problem is not None:
            return ToolResult(call, problem, is_error=True)
        try:
            content = tool.run(call.args)
        except ToolRefused as refusal:
            return ToolResult(call, str(refusal), is_error=True)
        return ToolResult(call, content, ends_run=tool.ends_run)
