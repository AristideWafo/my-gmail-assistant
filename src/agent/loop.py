import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.domain import (
    ANSWERED,
    ENDED_BY_TOOL,
    MODEL_ERROR,
    STEP_LIMIT,
    TOKEN_LIMIT,
    AgentMessage,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    Trajectory,
    Usage,
    UserMessage,
)
from src.errors import AgentModelError
from src.ports import AgentModel

logger = logging.getLogger(__name__)

TOOL_FAILED = "The tool failed."


@dataclass(frozen=True)
class Limits:
    max_steps: int
    max_tokens: int


class AgentLoop:
    """Lets the model call tools until it answers, a tool ends the run or a limit is reached."""

    def __init__(self, model: AgentModel, limits: Limits) -> None:
        self._model = model
        self._limits = limits

    def run(
        self,
        system: str,
        prompt: str,
        tools: Sequence[ToolSpec],
        execute: Callable[[ToolCall], ToolResult],
    ) -> Trajectory:
        messages: list[AgentMessage] = [UserMessage(prompt)]
        usage = Usage()

        def stopped(outcome: str, answer: str = "") -> Trajectory:
            return Trajectory(outcome, tuple(messages), usage, answer)

        for _ in range(self._limits.max_steps):
            try:
                turn = self._model.step(system, messages, tools)
            except AgentModelError as exc:
                logger.warning("Agent model gave no turn: %s", exc)
                return stopped(MODEL_ERROR)
            messages.append(turn)
            usage += turn.usage
            if not turn.tool_calls:
                return stopped(ANSWERED, turn.text)
            results = tuple(self._execute(execute, call) for call in turn.tool_calls)
            messages.append(ToolResults(results))
            if any(result.ends_run for result in results):
                return stopped(ENDED_BY_TOOL)
            if usage.tokens >= self._limits.max_tokens:
                return stopped(TOKEN_LIMIT)
        return stopped(STEP_LIMIT)

    @staticmethod
    def _execute(execute: Callable[[ToolCall], ToolResult], call: ToolCall) -> ToolResult:
        try:
            return execute(call)
        except Exception:
            # The model only learns that it failed: an exception text may carry what the tool
            # was not meant to return.
            logger.exception("Agent tool %s failed", call.name)
            return ToolResult(call, TOOL_FAILED, is_error=True)
