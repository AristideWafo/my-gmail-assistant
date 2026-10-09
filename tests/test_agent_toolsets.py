import json
import unittest
from datetime import timedelta

from src.agent import profiles
from src.agent.scope import NOT_READABLE
from src.agent.toolsets.judgment import MAX_QUESTIONS_PER_RUN, TOO_MANY_QUESTIONS
from src.agent.toolsets.mail import MAX_BODY_CHARS, MAX_HEADER_CHARS, MAX_THREAD_MESSAGES
from src.agent.toolsets.tracking import MAX_LISTED
from src.domain import WAITING_FOR_THEM, FollowUpAnchor, ToolCall, TrackedThread, TriageResult
from tests.agent_helpers import NOW, SECRET, email, world
from tests.followup_helpers import message, snapshot

QUESTION = {
    "question": "Does this mail ask for an answer?",
    "yes_means": "It asks something.",
    "no_means": "It asks nothing.",
}


class ToolsetTestCase(unittest.TestCase):
    def setUp(self):
        self.ports = world()
        self.addCleanup(self.ports.store.close)
        self.mail = self.ports.mail
        self.box = self.toolbox()

    def toolbox(self):
        return profiles.chat_read(self.ports)

    def call(self, name, **args):
        return self.box.execute(ToolCall(name, args))

    def output(self, name, **args):
        result = self.call(name, **args)
        self.assertFalse(result.is_error, result.content)
        return json.loads(result.content)


class ChatReadTests(ToolsetTestCase):
    def test_it_reads_searches_and_looks_at_what_was_recorded(self):
        names = {spec.name for spec in self.box.specs}

        self.assertEqual(
            names,
            {
                "read_mail",
                "read_thread",
                "search_mail",
                "ask_jev",
                "list_pending",
                "recall_decisions",
            },
        )

    def test_read_mail_returns_the_whole_mail(self):
        self.assertEqual(
            self.output("read_mail", message_id="m1"),
            {
                "message_id": "m1",
                "thread_id": "t1",
                "from": "jean@example.com",
                "subject": "Devis",
                "date": "2026-10-05",
                "body": "Où en est le devis ?",
            },
        )
        self.assertEqual(self.mail.called("fetch_message"), [("m1",)])

    def test_a_long_body_is_cut(self):
        self.mail.unread.append(email("m3", "t3", body="x" * (MAX_BODY_CHARS + 500)))

        self.assertLessEqual(len(self.output("read_mail", message_id="m3")["body"]), MAX_BODY_CHARS)

    def test_a_mail_that_does_not_exist_is_refused(self):
        result = self.call("read_mail", message_id="nope")

        self.assertEqual((result.content, result.is_error), (NOT_READABLE, True))

    def test_read_thread_returns_its_mails_oldest_first_with_who_wrote_them(self):
        thread = self.output("read_thread", thread_id="t1")

        self.assertEqual(thread["thread_id"], "t1")
        self.assertEqual([m["message_id"] for m in thread["messages"]], ["m1", "m2"])
        self.assertEqual([m["from_me"] for m in thread["messages"]], [False, True])
        self.assertEqual(thread["messages"][0]["to"], ["me@example.com"])
        self.assertEqual(thread["messages"][1]["body"], "Je relance.")

    def test_read_thread_keeps_only_the_last_mails_of_a_long_thread(self):
        ids = [f"x{i}" for i in range(MAX_THREAD_MESSAGES + 3)]
        self.mail.unread.extend(email(i, "long") for i in ids)
        self.mail.threads.append(snapshot(*(message(i) for i in ids), thread_id="long"))

        thread = self.output("read_thread", thread_id="long")

        self.assertEqual([m["message_id"] for m in thread["messages"]], ids[-MAX_THREAD_MESSAGES:])

    def test_a_mail_gone_since_the_thread_was_listed_is_left_out(self):
        self.mail.unread.pop(1)

        thread = self.output("read_thread", thread_id="t1")

        self.assertEqual([m["message_id"] for m in thread["messages"]], ["m1"])

    def test_headers_are_cut_like_bodies(self):
        hostile = self.output("read_mail", message_id="m2")

        self.assertLessEqual(len(hostile["subject"]), MAX_HEADER_CHARS)
        self.assertLessEqual(len(hostile["from"]), MAX_HEADER_CHARS)

    def test_a_thread_without_an_anchor_does_not_hide_the_others(self):
        for i in range(MAX_LISTED + 5):
            self.ports.store.threads.save(TrackedThread(f"n{i}", "1", WAITING_FOR_THEM, NOW))

        self.assertEqual([p["thread_id"] for p in self.output("list_pending")], ["t2"])

    def test_an_unknown_thread_is_refused(self):
        self.assertEqual(self.call("read_thread", thread_id="nope").content, NOT_READABLE)

    def test_search_returns_snippets_not_bodies(self):
        found = self.output("search_mail", query="salaires")

        self.assertEqual([m["message_id"] for m in found], ["m9"])
        self.assertEqual(found[0]["snippet"], SECRET)
        self.assertNotIn("body", found[0])

    def test_search_passes_the_limit_and_refuses_one_too_high(self):
        self.output("search_mail", query="devis", limit=1)

        self.assertEqual(self.mail.called("search"), [("devis", 1)])
        self.assertTrue(self.call("search_mail", query="devis", limit=500).is_error)

    def test_list_pending_shows_who_is_awaited(self):
        anchor = FollowUpAnchor("m2", NOW - timedelta(days=4), ("jean@example.com",), (), "Devis")
        self.ports.store.threads.save(
            TrackedThread(
                "t1", "1", WAITING_FOR_THEM, NOW, anchor=anchor, due_at=NOW, expects_answer=0.9
            )
        )

        pending = next(p for p in self.output("list_pending") if p["thread_id"] == "t1")

        self.assertEqual((pending["thread_id"], pending["to"]), ("t1", ["jean@example.com"]))
        self.assertEqual(pending["probability_it_expects_an_answer"], 0.9)
        self.assertEqual(pending["follow_up_due_at"], NOW.isoformat())

    def test_recall_decisions_matches_part_of_the_sender(self):
        store = self.ports.store
        store.record_decision(email("m1", "t1"), TriageResult("high", "personnel", 0.9), "llm")
        store.record_decision(
            email("m9", "t2", sender="rh@example.com"),
            TriageResult("low", "newsletter", 0.9),
            "reject",
        )

        recalled = self.output("recall_decisions", sender="JEAN@")

        self.assertEqual([(r["message_id"], r["route"]) for r in recalled], [("m1", "llm")])


class AskJevTests(ToolsetTestCase):
    def test_it_asks_about_the_mail_and_returns_the_probability(self):
        self.ports.judge.answer = 0.8765

        self.assertEqual(
            self.output("ask_jev", message_id="m1", **QUESTION), {"probability_yes": 0.88}
        )
        state, question, yes, no = self.ports.judge.asked[0]
        self.assertEqual(
            state,
            {
                "subject": "Devis",
                "body": "Où en est le devis ?",
                "sender": "jean@example.com",
                "received_at": "2026-10-05",
                "today": "2026-10-09",
            },
        )
        self.assertEqual((question, yes, no), tuple(QUESTION.values()))

    def test_questions_are_capped_per_run(self):
        for _ in range(MAX_QUESTIONS_PER_RUN):
            self.output("ask_jev", message_id="m1", **QUESTION)

        refused = self.call("ask_jev", message_id="m1", **QUESTION)

        self.assertEqual((refused.content, refused.is_error), (TOO_MANY_QUESTIONS, True))
        self.assertEqual(len(self.ports.judge.asked), MAX_QUESTIONS_PER_RUN)

    def test_a_refused_mail_does_not_use_up_a_question(self):
        for _ in range(MAX_QUESTIONS_PER_RUN + 2):
            self.call("ask_jev", message_id="nope", **QUESTION)

        self.assertFalse(self.call("ask_jev", message_id="m1", **QUESTION).is_error)

    def test_each_run_gets_its_own_count(self):
        for _ in range(MAX_QUESTIONS_PER_RUN):
            self.output("ask_jev", message_id="m1", **QUESTION)

        self.box = self.toolbox()

        self.assertFalse(self.call("ask_jev", message_id="m1", **QUESTION).is_error)

    def test_without_a_configured_judge_the_tool_does_not_exist(self):
        self.ports.judge.is_configured = False

        self.assertNotIn("ask_jev", {spec.name for spec in self.toolbox().specs})


class ThreadBoundTests(ToolsetTestCase):
    def toolbox(self):
        return profiles.thread_bound(self.ports, "t1")

    def test_it_can_read_and_judge_but_not_search_or_look_elsewhere(self):
        self.assertEqual(
            {spec.name for spec in self.box.specs}, {"read_mail", "read_thread", "ask_jev"}
        )

    def test_its_own_thread_is_readable(self):
        self.assertEqual(len(self.output("read_thread", thread_id="t1")["messages"]), 2)
        self.assertEqual(self.output("read_mail", message_id="m2")["thread_id"], "t1")

    def test_another_thread_is_refused_before_the_provider_is_asked(self):
        result = self.call("read_thread", thread_id="t2")

        self.assertEqual((result.content, result.is_error), (NOT_READABLE, True))
        self.assertEqual(self.mail.called("thread_snapshot"), [])

    def test_a_mail_of_another_thread_is_refused_like_one_that_does_not_exist(self):
        elsewhere = self.call("read_mail", message_id="m9")
        missing = self.call("read_mail", message_id="nope")

        self.assertEqual(elsewhere.content, missing.content)
        self.assertNotIn(SECRET, elsewhere.content)

    def test_a_provider_answering_with_another_thread_is_not_believed(self):
        self.mail.threads[0] = snapshot(message("m1"), thread_id="t2")

        self.assertEqual(self.call("read_thread", thread_id="t1").content, NOT_READABLE)

    def test_a_mail_of_another_thread_listed_in_this_one_is_left_out(self):
        self.mail.threads[0] = snapshot(message("m1"), message("m9"))

        thread = self.output("read_thread", thread_id="t1")

        self.assertEqual([m["message_id"] for m in thread["messages"]], ["m1"])

    def test_a_run_cannot_be_bound_to_an_empty_thread_id(self):
        with self.assertRaises(ValueError):
            profiles.thread_bound(self.ports, "")

    def test_jev_is_not_asked_about_a_mail_of_another_thread(self):
        result = self.call("ask_jev", message_id="m9", **QUESTION)

        self.assertTrue(result.is_error)
        self.assertEqual(self.ports.judge.asked, [])

    def test_a_run_bound_to_the_other_thread_reads_that_one_only(self):
        self.box = profiles.thread_bound(self.ports, "t2")

        self.assertTrue(self.call("read_mail", message_id="m1").is_error)
        self.assertEqual(self.output("read_mail", message_id="m9")["body"], SECRET)


if __name__ == "__main__":
    unittest.main()
