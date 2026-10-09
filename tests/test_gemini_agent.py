import unittest
from unittest.mock import MagicMock, patch

import httpx
from google.genai import errors, types

from src.domain import AgentTurn, ToolCall, ToolResult, ToolResults, ToolSpec, Usage, UserMessage
from src.errors import AgentModelError
from src.llm.gemini_agent import GeminiAgentModel
from src.ports import AgentModel

SEARCH = ToolSpec(
    "search_mail",
    "Searches the mailbox.",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)
FIND = ToolCall("search_mail", {"query": "devis"}, id="c1")


def response(*parts, prompt=100, candidates=20, thoughts=0):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            thoughts_token_count=thoughts,
        ),
    )


def call_part(name="search_mail", args=None, call_id=None):
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args or {}))


class GeminiAgentModelTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.limiter = MagicMock()
        self.model = GeminiAgentModel(
            "key", "gemini-2.5-flash", limiter=self.limiter, client=self.client
        )
        metrics = patch("src.llm.gemini_agent.Metrics")
        self.metrics = metrics.start()
        self.addCleanup(metrics.stop)

    def answer(self, *parts, **usage):
        self.client.models.generate_content.return_value = response(*parts, **usage)

    def request(self):
        return self.client.models.generate_content.call_args.kwargs

    def test_is_an_agent_model(self):
        self.assertIsInstance(self.model, AgentModel)

    def test_without_a_key_it_is_not_configured_and_gives_no_turn(self):
        model = GeminiAgentModel("", "gemini-2.5-flash")

        self.assertFalse(model.is_configured)
        with self.assertRaises(AgentModelError):
            model.step("system", [UserMessage("hi")], [])

    def test_a_key_builds_a_client_with_the_timeout_in_milliseconds(self):
        with patch("src.llm.gemini_agent.genai.Client") as client:
            model = GeminiAgentModel("key", "gemini-2.5-flash", timeout_seconds=12.5)

        self.assertTrue(model.is_configured)
        self.assertEqual(client.call_args.kwargs["api_key"], "key")
        self.assertEqual(client.call_args.kwargs["http_options"].timeout, 12500)

    def test_text_is_returned_as_the_answer_with_its_usage_and_cost(self):
        self.answer(types.Part.from_text(text="Rien en attente."), prompt=1_000_000, candidates=0)

        turn = self.model.step("system", [UserMessage("hi")], [])

        self.assertEqual((turn.text, turn.tool_calls), ("Rien en attente.", ()))
        self.assertEqual(turn.usage, Usage(1_000_000, 0, 0.30))
        self.metrics.mark_llm_usage.assert_called_once_with("agent", 1_000_000, 0, 0.30)
        self.limiter.acquire.assert_called_once_with()

    def test_thinking_is_left_out_of_the_text_and_counted_as_output(self):
        self.answer(
            types.Part(text="let me think", thought=True),
            types.Part.from_text(text="Oui."),
            candidates=20,
            thoughts=30,
        )

        turn = self.model.step("system", [UserMessage("hi")], [])

        self.assertEqual(turn.text, "Oui.")
        self.assertEqual(turn.usage.output_tokens, 50)

    def test_function_calls_become_tool_calls(self):
        self.answer(call_part(args={"query": "devis"}, call_id="c1"), call_part("read_thread"))

        turn = self.model.step("system", [UserMessage("hi")], [SEARCH])

        self.assertEqual(turn.tool_calls, (FIND, ToolCall("read_thread", {}, id="")))

    def test_the_request_declares_the_tools_and_never_lets_the_sdk_run_them(self):
        self.answer(types.Part.from_text(text="ok"))

        self.model.step("Tu es un assistant.", [UserMessage("hi")], [SEARCH])

        request = self.request()
        config = request["config"]
        declaration, = config.tools[0].function_declarations
        self.assertEqual(request["model"], "gemini-2.5-flash")
        self.assertEqual(config.system_instruction, "Tu es un assistant.")
        self.assertEqual(
            (declaration.name, declaration.description, declaration.parameters_json_schema),
            (SEARCH.name, SEARCH.description, SEARCH.parameters),
        )
        self.assertIs(config.automatic_function_calling.disable, True)

    def test_without_tools_none_is_declared(self):
        self.answer(types.Part.from_text(text="ok"))

        self.model.step("system", [UserMessage("hi")], [])

        self.assertIsNone(self.request()["config"].tools)

    def test_the_conversation_is_replayed_with_the_models_own_turn(self):
        raw = types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(name="search_mail", args={}),
                    thought_signature=b"sig",
                )
            ],
        )
        self.answer(types.Part.from_text(text="ok"))
        history = [
            UserMessage("où en est le devis ?"),
            AgentTurn(tool_calls=(FIND,), raw=raw),
            ToolResults((ToolResult(FIND, "2 mails"), ToolResult(FIND, "boom", is_error=True))),
        ]

        self.model.step("system", history, [SEARCH])

        user, model, results = self.request()["contents"]
        self.assertEqual((user.role, user.parts[0].text), ("user", "où en est le devis ?"))
        self.assertIs(model, raw)
        self.assertEqual(results.role, "user")
        found, failed = (part.function_response for part in results.parts)
        self.assertEqual(
            (found.id, found.name, found.response), ("c1", "search_mail", {"output": "2 mails"})
        )
        self.assertEqual(failed.response, {"error": "boom"})

    def test_a_turn_from_elsewhere_is_rebuilt_from_its_text_and_calls(self):
        self.answer(types.Part.from_text(text="ok"))
        turn = AgentTurn(text="Je cherche.", tool_calls=(ToolCall("search_mail", {"query": "x"}),))

        self.model.step("system", [UserMessage("hi"), turn], [SEARCH])

        rebuilt = self.request()["contents"][1]
        self.assertEqual(rebuilt.role, "model")
        self.assertEqual(rebuilt.parts[0].text, "Je cherche.")
        call = rebuilt.parts[1].function_call
        self.assertEqual((call.id, call.name, call.args), (None, "search_mail", {"query": "x"}))

    def test_an_empty_or_blocked_answer_is_an_error_not_a_silent_answer(self):
        for blocked in (types.GenerateContentResponse(candidates=[]), response()):
            with self.subTest(candidates=len(blocked.candidates)):
                self.client.models.generate_content.return_value = blocked

                with self.assertRaises(AgentModelError):
                    self.model.step("system", [UserMessage("hi")], [])

        self.metrics.mark_llm_error.assert_called_with("empty")

    def test_failures_are_counted_by_cause_and_hide_the_provider_message(self):
        cases = [
            (errors.APIError(429, {"error": {"message": "quota for key AIza"}}), "rate_limited"),
            (errors.APIError(503, {"error": {"message": "overloaded"}}), "unavailable"),
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectError("dns"), "unavailable"),
        ]
        for failure, reason in cases:
            with self.subTest(reason=reason, failure=type(failure).__name__):
                self.client.models.generate_content.side_effect = failure

                with self.assertRaises(AgentModelError) as raised:
                    self.model.step("system", [UserMessage("hi")], [])

                self.metrics.mark_llm_error.assert_called_with(reason)
                self.assertNotIn("AIza", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_a_model_without_a_verified_price_has_no_cost(self):
        model = GeminiAgentModel("key", "gemini-unpriced", limiter=self.limiter, client=self.client)
        self.answer(types.Part.from_text(text="ok"))

        with self.assertLogs("src.llm.pricing", level="WARNING"):
            turn = model.step("system", [UserMessage("hi")], [])

        self.assertIsNone(turn.usage.cost_usd)

    def test_missing_usage_counts_nothing(self):
        self.client.models.generate_content.return_value = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part.from_text(text="ok")])
                )
            ]
        )

        self.assertEqual(self.model.step("system", [UserMessage("hi")], []).usage, Usage())
        self.metrics.mark_llm_usage.assert_not_called()

    def test_check_connection_asks_for_the_model(self):
        self.assertEqual(self.model.check_connection(), "model gemini-2.5-flash available")
        self.client.models.get.assert_called_once_with(model="gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
