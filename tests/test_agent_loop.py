import unittest

from src.agent.loop import TOOL_FAILED, AgentLoop, Limits
from src.domain import (
    ANSWERED,
    ENDED_BY_TOOL,
    MODEL_ERROR,
    STEP_LIMIT,
    TOKEN_LIMIT,
    AgentTurn,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    Usage,
    UserMessage,
)
from src.errors import AgentModelError
from tests.fakes import FakeAgentModel

SEARCH = ToolSpec("search_mail", "Searches the mailbox.", {"type": "object", "properties": {}})
FIND = ToolCall("search_mail", {"query": "devis"}, id="c1")
READ = ToolCall("read_thread", {"thread_id": "t1"}, id="c2")


def calling(*calls, tokens=10):
    return AgentTurn(tool_calls=calls, usage=Usage(tokens, 0))


def saying(text, tokens=10):
    return AgentTurn(text=text, usage=Usage(tokens, 0))


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.executed = []

    def execute(self, call):
        self.executed.append(call)
        return ToolResult(call, f"result of {call.name}")

    def run_loop(self, *script, max_steps=6, max_tokens=1000, execute=None):
        self.model = FakeAgentModel(list(script))
        loop = AgentLoop(self.model, Limits(max_steps, max_tokens))
        return loop.run("system", "où en est le devis ?", [SEARCH], execute or self.execute)

    def test_an_answer_without_a_tool_ends_the_run(self):
        trajectory = self.run_loop(saying("Rien en attente."))

        self.assertEqual((trajectory.outcome, trajectory.answer), (ANSWERED, "Rien en attente."))
        self.assertEqual(self.executed, [])
        self.assertEqual(
            self.model.seen, [("system", (UserMessage("où en est le devis ?"),), (SEARCH,))]
        )

    def test_tool_results_go_back_to_the_model_until_it_answers(self):
        trajectory = self.run_loop(calling(FIND), calling(READ), saying("Jean a répondu hier."))

        self.assertEqual(trajectory.outcome, ANSWERED)
        self.assertEqual(self.executed, [FIND, READ])
        self.assertEqual(trajectory.tool_calls, (FIND, READ))
        shown_last = self.model.seen[-1][1]
        self.assertEqual(
            shown_last[1:],
            (
                calling(FIND),
                ToolResults((ToolResult(FIND, "result of search_mail"),)),
                calling(READ),
                ToolResults((ToolResult(READ, "result of read_thread"),)),
            ),
        )

    def test_several_calls_in_one_turn_are_all_run_in_order(self):
        trajectory = self.run_loop(calling(FIND, READ), saying("ok"))

        self.assertEqual(self.executed, [FIND, READ])
        self.assertEqual(len(trajectory.messages[2].results), 2)

    def test_a_tool_that_ends_the_run_gives_the_model_no_further_turn(self):
        def propose(call):
            return ToolResult(call, "proposed", ends_run=True)

        trajectory = self.run_loop(calling(FIND), saying("never said"), execute=propose)

        self.assertEqual((trajectory.outcome, trajectory.answer), (ENDED_BY_TOOL, ""))
        self.assertEqual(len(self.model.seen), 1)

    def test_the_run_stops_at_the_step_limit(self):
        trajectory = self.run_loop(calling(FIND), calling(FIND), calling(FIND), max_steps=2)

        self.assertEqual(trajectory.outcome, STEP_LIMIT)
        self.assertEqual(len(self.executed), 2)

    def test_the_run_stops_once_the_token_limit_is_reached(self):
        trajectory = self.run_loop(
            calling(FIND, tokens=60), calling(FIND, tokens=60), saying("late"), max_tokens=100
        )

        self.assertEqual(trajectory.outcome, TOKEN_LIMIT)
        self.assertEqual(trajectory.usage.tokens, 120)
        self.assertEqual(len(self.model.seen), 2)

    def test_an_answer_is_kept_even_when_it_crosses_the_token_limit(self):
        trajectory = self.run_loop(saying("Voici.", tokens=500), max_tokens=100)

        self.assertEqual((trajectory.outcome, trajectory.answer), (ANSWERED, "Voici."))

    def test_a_model_failure_ends_the_run_with_what_was_done(self):
        trajectory = self.run_loop(calling(FIND), AgentModelError("status 503"))

        self.assertEqual(trajectory.outcome, MODEL_ERROR)
        self.assertEqual(trajectory.tool_calls, (FIND,))
        self.assertEqual(trajectory.usage, Usage(10, 0))

    def test_a_failing_tool_is_reported_to_the_model_without_its_error_text(self):
        def explode(call):
            raise RuntimeError("token=secret")

        with self.assertLogs("src.agent.loop", level="ERROR"):
            trajectory = self.run_loop(calling(FIND), saying("Échec."), execute=explode)

        self.assertEqual(trajectory.outcome, ANSWERED)
        result, = trajectory.messages[2].results
        self.assertEqual((result.content, result.is_error), (TOOL_FAILED, True))

    def test_usage_adds_up_over_the_turns(self):
        trajectory = self.run_loop(
            AgentTurn(tool_calls=(FIND,), usage=Usage(100, 20, 0.001)),
            AgentTurn(text="ok", usage=Usage(150, 30, 0.002)),
        )

        self.assertEqual(trajectory.usage.input_tokens, 250)
        self.assertEqual(trajectory.usage.output_tokens, 50)
        self.assertAlmostEqual(trajectory.usage.cost_usd, 0.003)


class UsageTests(unittest.TestCase):
    def test_cost_stays_unknown_when_no_turn_has_a_price(self):
        self.assertIsNone((Usage(1, 1) + Usage(2, 2)).cost_usd)

    def test_a_known_cost_is_kept_next_to_an_unknown_one(self):
        self.assertEqual((Usage(1, 1, 0.5) + Usage(2, 2)).cost_usd, 0.5)


if __name__ == "__main__":
    unittest.main()
