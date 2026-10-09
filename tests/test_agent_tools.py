import unittest

from src.agent.schema import arguments, integer, problem_with, text
from src.agent.tools import UNKNOWN_TOOL, Tool, Toolbox, ToolRefused
from src.domain import ToolCall, ToolSpec

SCHEMA = arguments(
    optional=("limit",),
    query=text("What to look for.", 20),
    limit=integer("Most results.", 1, 10),
)


class SchemaTests(unittest.TestCase):
    def test_arguments_that_fit_have_no_problem(self):
        for args in ({"query": "devis"}, {"query": "devis", "limit": 10}):
            with self.subTest(args=args):
                self.assertIsNone(problem_with(SCHEMA, args))

    def test_what_is_declared_is_what_is_checked(self):
        self.assertEqual(SCHEMA["required"], ["query"])
        self.assertIs(SCHEMA["additionalProperties"], False)
        self.assertEqual(SCHEMA["properties"]["query"]["maxLength"], 20)

    def test_each_misfit_is_named(self):
        cases = [
            ("not an object", "Arguments must be an object."),
            (None, "Arguments must be an object."),
            ({}, "Missing argument: query."),
            ({"query": "x", "to": "evil@example.com"}, "Unknown argument: to."),
            ({"query": 3}, "Argument query must be of type string."),
            ({"query": ["a"]}, "Argument query must be of type string."),
            ({"query": "   "}, "Argument query must not be empty."),
            ({"query": "x" * 21}, "Argument query must be at most 20 characters."),
            ({"query": "x", "limit": "3"}, "Argument limit must be of type integer."),
            ({"query": "x", "limit": True}, "Argument limit must be of type integer."),
            ({"query": "x", "limit": 0}, "Argument limit must be between 1 and 10."),
            ({"query": "x", "limit": 11}, "Argument limit must be between 1 and 10."),
            ({"query": "x", 7: "y"}, "Unknown argument: 7."),
        ]
        for args, problem in cases:
            with self.subTest(args=args):
                self.assertEqual(problem_with(SCHEMA, args), problem)

    def test_a_long_unknown_name_is_cut_in_the_message(self):
        problem = problem_with(SCHEMA, {"query": "x", "z" * 500: 1})

        self.assertLess(len(problem), 70)


class ToolboxTests(unittest.TestCase):
    def setUp(self):
        self.ran = []
        self.box = Toolbox(
            [
                Tool(ToolSpec("search", "Searches.", SCHEMA), self.search),
                Tool(ToolSpec("propose", "Proposes.", arguments()), lambda _: "ok", ends_run=True),
            ]
        )

    def search(self, args):
        self.ran.append(args)
        if args["query"] == "forbidden":
            raise ToolRefused("Not this one.")
        return "2 results"

    def test_specs_are_those_of_its_tools(self):
        self.assertEqual([spec.name for spec in self.box.specs], ["search", "propose"])

    def test_a_known_tool_runs_with_its_arguments(self):
        call = ToolCall("search", {"query": "devis"})

        result = self.box.execute(call)

        self.assertEqual((result.call, result.content, result.is_error), (call, "2 results", False))
        self.assertEqual(self.ran, [{"query": "devis"}])

    def test_a_name_outside_the_box_does_not_exist(self):
        for name in ("send_draft", "", "SEARCH", None, 3):
            with self.subTest(name=name):
                result = self.box.execute(ToolCall(name, {"query": "devis"}))

                self.assertEqual((result.content, result.is_error), (UNKNOWN_TOOL, True))
        self.assertEqual(self.ran, [])

    def test_arguments_that_do_not_fit_never_reach_the_tool(self):
        result = self.box.execute(ToolCall("search", {"query": "devis", "to": "x@example.com"}))

        self.assertEqual((result.content, result.is_error), ("Unknown argument: to.", True))
        self.assertEqual(self.ran, [])

    def test_a_refusal_is_told_to_the_model_as_an_error(self):
        result = self.box.execute(ToolCall("search", {"query": "forbidden"}))

        self.assertEqual(
            (result.content, result.is_error, result.ends_run), ("Not this one.", True, False)
        )

    def test_only_a_tool_made_for_it_ends_the_run(self):
        self.assertFalse(self.box.execute(ToolCall("search", {"query": "devis"})).ends_run)
        self.assertTrue(self.box.execute(ToolCall("propose", {})).ends_run)

    def test_an_ending_tool_called_wrongly_does_not_end_the_run(self):
        self.assertFalse(self.box.execute(ToolCall("propose", {"x": 1})).ends_run)

    def test_two_tools_cannot_share_a_name(self):
        tool = Tool(ToolSpec("search", "Searches.", SCHEMA), self.search)

        with self.assertRaises(ValueError):
            Toolbox([tool, tool])


if __name__ == "__main__":
    unittest.main()
