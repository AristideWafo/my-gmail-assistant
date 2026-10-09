from dataclasses import dataclass, field

ANSWERED = "answered"
ENDED_BY_TOOL = "ended_by_tool"
STEP_LIMIT = "step_limit"
TOKEN_LIMIT = "token_limit"
MODEL_ERROR = "model_error"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema of the arguments, as an object.
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    # Empty when the provider gives calls no id; results are then matched by order.
    id: str = ""


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    content: str
    is_error: bool = False
    # The tool did what the run was for: the model gets no further turn.
    ends_run: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    def __add__(self, other: "Usage") -> "Usage":
        costs = [cost for cost in (self.cost_usd, other.cost_usd) if cost is not None]
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            sum(costs) if costs else None,
        )

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AgentTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Usage()
    # The provider's own form of this turn, replayed as is: it may hold what the neutral
    # fields cannot, such as the signatures a model needs to continue after a tool call.
    raw: object = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ToolResults:
    results: tuple[ToolResult, ...]


AgentMessage = UserMessage | AgentTurn | ToolResults


@dataclass(frozen=True)
class Trajectory:
    """Everything one run said and did, in order, and how it stopped."""

    outcome: str
    messages: tuple[AgentMessage, ...]
    usage: Usage = Usage()
    answer: str = ""

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(
            call
            for message in self.messages
            if isinstance(message, AgentTurn)
            for call in message.tool_calls
        )
