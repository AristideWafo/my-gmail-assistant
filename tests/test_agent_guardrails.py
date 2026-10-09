"""A model that calls anything with anything must get nowhere: these properties hold whatever
the model was told, tricked into or decides. They are the reason a run may read untrusted mail."""

import random
import unittest
from dataclasses import dataclass

from src.agent import profiles
from src.agent.loop import TOOL_FAILED, AgentLoop, Limits
from src.agent.profiles import AgentPorts
from src.agent.scope import NOT_READABLE
from src.domain import AgentTurn, ToolCall, ToolResult, ToolResults
from tests.agent_helpers import SECRET, world
from tests.fakes import FakeAgentModel

TURNS = 80
CALLS_PER_TURN = 5
# Eight mails cut at 3 000 characters each, plus their headers.
MAX_RESULT_CHARS = 40_000
KNOWN = ("read_mail", "read_thread", "search_mail", "ask_jev", "list_pending", "recall_decisions")
# What a hijacked model would reach for: the ports' own method names and likely tool names.
FORBIDDEN = (
    "send_draft", "create_draft", "archive_message", "label_message", "send_mail", "send",
    "reply", "forward", "delete", "remember", "propose_rule", "set_state", "claim", "unsubscribe",
    "fetch_message", "thread_snapshot", "search", "__class__", "", "READ_MAIL", "read_mail ",
)  # fmt: skip
IDS = ("m1", "m2", "m9", "t1", "t2", "nope", "", "../t2", "t1,t2", "*", "m9\x00", "x" * 5000)
JUNK = (None, 0, -1, 10**9, True, 3.5, [], ["m9"], {}, {"thread_id": "t2"}, "to:evil@example.com")
ARGUMENTS = (
    "message_id", "thread_id", "query", "limit", "sender", "question", "yes_means", "no_means",
    "to", "cc", "bcc", "body", "label", "scope", "thread_ids", "draft_id", "url",
)  # fmt: skip


def arbitrary_calls(seed):
    rng = random.Random(seed)
    for _ in range(TURNS * CALLS_PER_TURN):
        name = rng.choice(KNOWN * 3 + FORBIDDEN)
        args = {rng.choice(ARGUMENTS): rng.choice(IDS + JUNK) for _ in range(rng.randint(0, 4))}
        # Half of the calls to a real tool are well formed, so its guards are reached too.
        if name in KNOWN and rng.random() < 0.5:
            args = well_formed(name, rng)
        yield ToolCall(name, args if rng.random() < 0.97 else rng.choice(JUNK))


def well_formed(name, rng):
    message_id = rng.choice(("m1", "m2", "m9", "nope"))
    thread_id = rng.choice(("t1", "t2", "nope"))
    return {
        "read_mail": {"message_id": message_id},
        "read_thread": {"thread_id": thread_id},
        "search_mail": {"query": rng.choice(("salaires", "devis", "in:anywhere", "from:rh"))},
        "ask_jev": {"message_id": message_id, "question": "q?", "yes_means": "y", "no_means": "n"},
        "list_pending": {},
        "recall_decisions": {"sender": rng.choice(("rh.fr", "jean", "example.com"))},
    }[name]


PROFILES = {
    "chat_read": profiles.chat_read,
    "thread_bound": lambda ports: profiles.thread_bound(ports, "t1"),
}
SEEDS = range(5)


@dataclass
class Run:
    profile: str
    seed: int
    ports: AgentPorts
    offered: frozenset[str]
    results: list[ToolResult]
    store_before: list[str]

    @property
    def bound_to_a_thread(self):
        return self.profile != "chat_read"


def run_adversary(profile, seed):
    ports = world()
    calls = list(arbitrary_calls(seed))
    script = [
        AgentTurn(tool_calls=tuple(calls[i : i + CALLS_PER_TURN]))
        for i in range(0, len(calls), CALLS_PER_TURN)
    ]
    toolbox = PROFILES[profile](ports)
    store_before = list(ports.store._conn.iterdump())
    loop = AgentLoop(FakeAgentModel(script), Limits(max_steps=TURNS, max_tokens=10**9))
    trajectory = loop.run("system", "prompt", toolbox.specs, toolbox.execute)
    results = [
        result
        for message in trajectory.messages
        if isinstance(message, ToolResults)
        for result in message.results
    ]
    assert len(results) == len(calls)
    offered = frozenset(spec.name for spec in toolbox.specs)
    return Run(profile, seed, ports, offered, results, store_before)


class AdversarialModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = [run_adversary(profile, seed) for profile in PROFILES for seed in SEEDS]

    @classmethod
    def tearDownClass(cls):
        for run in cls.runs:
            run.ports.store.close()

    def check_each_run(self, check, only_bound=False):
        for run in self.runs:
            if only_bound and not run.bound_to_a_thread:
                continue
            with self.subTest(profile=run.profile, seed=run.seed):
                check(run)

    def test_nothing_is_ever_written_to_the_mailbox(self):
        self.check_each_run(lambda run: self.assertEqual(run.ports.mail.writes, []))

    def test_the_store_is_left_exactly_as_it_was(self):
        self.check_each_run(
            lambda run: self.assertEqual(list(run.ports.store._conn.iterdump()), run.store_before)
        )

    def test_no_result_is_large_whatever_the_mail_holds(self):
        self.check_each_run(
            lambda run: self.assertLess(max(len(r.content) for r in run.results), MAX_RESULT_CHARS)
        )

    def test_a_name_the_profile_does_not_offer_always_fails(self):
        def check(run):
            wrongly_run = [
                result.call
                for result in run.results
                if result.call.name not in run.offered and not result.is_error
            ]
            self.assertEqual(wrongly_run, [])

        self.check_each_run(check)

    def test_a_malformed_call_is_refused_not_crashed_on(self):
        self.check_each_run(
            lambda run: self.assertNotIn(TOOL_FAILED, [result.content for result in run.results])
        )

    def test_a_run_bound_to_a_thread_never_sees_another_one(self):
        def check(run):
            mail, judge = run.ports.mail, run.ports.judge
            self.assertEqual([r.call for r in run.results if SECRET in r.content], [])
            self.assertEqual(mail.called("search"), [])
            self.assertLessEqual({args[0] for args in mail.called("thread_snapshot")}, {"t1"})
            self.assertEqual([state for state, *_ in judge.asked if SECRET in str(state)], [])

        self.check_each_run(check, only_bound=True)

    def test_the_whole_mailbox_is_only_for_a_run_the_user_started(self):
        self.check_each_run(
            lambda run: self.assertEqual("search_mail" in run.offered, not run.bound_to_a_thread)
        )

    def test_the_adversary_does_get_past_argument_checks(self):
        # Otherwise the properties above could hold on a suite that never reached a guard.
        def reached_tools(run):
            succeeded = {result.call.name for result in run.results if not result.is_error}
            self.assertLessEqual({"read_mail", "read_thread", "ask_jev"}, succeeded)

        def reached_scope(run):
            refused = [result.call for result in run.results if result.is_error]
            self.assertIn(ToolCall("read_mail", {"message_id": "m9"}), refused)
            self.assertIn(ToolCall("read_thread", {"thread_id": "t2"}), refused)

        self.check_each_run(reached_tools)
        self.check_each_run(reached_scope, only_bound=True)

    def test_the_adversary_does_ask_jev_about_the_other_thread(self):
        asked_out_of_reach = [
            result
            for run in self.runs
            if run.bound_to_a_thread
            for result in run.results
            if result.call.name == "ask_jev"
            and isinstance(result.call.args, dict)
            and result.call.args.get("message_id") == "m9"
            and result.content == NOT_READABLE
        ]
        self.assertTrue(asked_out_of_reach)

    def test_the_user_started_run_does_read_what_the_store_holds(self):
        # The secret stands for any private text: a run the user started is allowed to see it.
        for run in self.runs:
            if not run.bound_to_a_thread:
                seen = {r.call.name for r in run.results if SECRET in r.content}
                self.assertLessEqual({"list_pending", "recall_decisions", "read_mail"}, seen)


if __name__ == "__main__":
    unittest.main()
