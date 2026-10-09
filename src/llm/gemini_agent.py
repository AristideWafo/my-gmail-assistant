import logging
from collections.abc import Sequence

import httpx
from google import genai
from google.genai import errors, types

from src.domain import (
    AgentMessage,
    AgentTurn,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    Usage,
    UserMessage,
)
from src.errors import AgentModelError
from src.llm.pricing import estimate_cost_usd
from src.llm.rate_limit import RateLimiter
from src.observability.metrics import Metrics

logger = logging.getLogger(__name__)

USAGE_KIND = "agent"
RATE_LIMITED_STATUS = 429


class GeminiAgentModel:
    def __init__(
        self,
        api_key: str,
        model_name: str,
        max_rpm: int = 12,
        timeout_seconds: float = 30.0,
        limiter: RateLimiter | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self.model_name = model_name
        self._limiter = limiter or RateLimiter(max_rpm)
        self._client = client
        if client is None and api_key:
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
            )

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    def check_connection(self) -> str:
        self._client.models.get(model=self.model_name)
        return f"model {self.model_name} available"

    def step(
        self, system: str, messages: Sequence[AgentMessage], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        if self._client is None:
            raise AgentModelError("the agent model is not configured")
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=_declarations(tools),
            # The loop runs the tools, under its own limits and guards: the SDK must not.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self._limiter.acquire()
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=[_content(message) for message in messages],
                config=config,
            )
        except httpx.TimeoutException:
            Metrics.mark_llm_error("timeout")
            raise AgentModelError("the agent model timed out") from None
        except errors.APIError as exc:
            rate_limited = exc.code == RATE_LIMITED_STATUS
            Metrics.mark_llm_error("rate_limited" if rate_limited else "unavailable")
            raise AgentModelError(f"the agent model failed (status {exc.code})") from None
        except httpx.HTTPError as exc:
            Metrics.mark_llm_error("unavailable")
            raise AgentModelError(f"the agent model failed ({type(exc).__name__})") from None
        usage = self._usage(response)
        content = response.candidates[0].content if response.candidates else None
        if content is None or not content.parts:
            # A blocked or empty candidate is not an answer: saying nothing must not pass for one.
            Metrics.mark_llm_error("empty")
            raise AgentModelError("the agent model returned no content")
        return AgentTurn(
            text="".join(part.text for part in content.parts if part.text and not part.thought),
            tool_calls=tuple(
                ToolCall(
                    name=part.function_call.name,
                    args=dict(part.function_call.args or {}),
                    id=part.function_call.id or "",
                )
                for part in content.parts
                if part.function_call is not None
            ),
            usage=usage,
            raw=content,
        )

    def _usage(self, response: types.GenerateContentResponse) -> Usage:
        metadata = response.usage_metadata
        if metadata is None:
            return Usage()
        prompt = metadata.prompt_token_count or 0
        # Thinking is billed as output.
        completion = (metadata.candidates_token_count or 0) + (metadata.thoughts_token_count or 0)
        cost_usd = estimate_cost_usd(self.model_name, prompt, completion)
        Metrics.mark_llm_usage(USAGE_KIND, prompt, completion, cost_usd)
        return Usage(prompt, completion, cost_usd)


def _declarations(tools: Sequence[ToolSpec]) -> list[types.Tool] | None:
    if not tools:
        return None
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.parameters,
                )
                for tool in tools
            ]
        )
    ]


def _content(message: AgentMessage) -> types.Content:
    if isinstance(message, UserMessage):
        return types.Content(role="user", parts=[types.Part.from_text(text=message.text)])
    if isinstance(message, ToolResults):
        return types.Content(role="user", parts=[_response(result) for result in message.results])
    if isinstance(message.raw, types.Content):
        return message.raw
    parts = [types.Part.from_text(text=message.text)] if message.text else []
    parts += [
        types.Part(
            function_call=types.FunctionCall(id=call.id or None, name=call.name, args=call.args)
        )
        for call in message.tool_calls
    ]
    return types.Content(role="model", parts=parts)


def _response(result: ToolResult) -> types.Part:
    return types.Part(
        function_response=types.FunctionResponse(
            id=result.call.id or None,
            name=result.call.name,
            response={"error" if result.is_error else "output": result.content},
        )
    )
