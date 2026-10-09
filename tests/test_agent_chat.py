import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from main import ApplicationContext
from src.agent.chat import (
    GAVE_UP,
    GAVE_UP_OTHER,
    KIND,
    MAX_MESSAGE_CHARS,
    MAX_QUESTION_CHARS,
    NO_ANSWER,
    ChatRuns,
    payload,
    presentable,
    trigger_key,
)
from src.agent.loop import Limits
from src.agent.prompts import chat_system
from src.agent.worker import AgentBudget, AgentWorker
from src.bootstrap import build_components
from src.config import Settings
from src.domain import ANSWERED, RUN_DONE, AgentTurn, TextEvent, ToolCall, UserMessage
from src.errors import AgentModelError
from src.formatting import LINK_REMOVED
from tests.agent_helpers import NOW, world
from tests.fakes import FakeAgentModel, FakeChat, FakeMail, fake_components

LIMITS = Limits(max_steps=3, max_tokens=10_000)
QUESTION = "Où en est le devis ?"


class PresentableTests(unittest.TestCase):
    def test_a_link_from_a_mail_is_removed(self):
        answer = "Votre colis attend : https://suivi.example/confirm?id=1&mail=me. Cliquez vite."

        self.assertEqual(
            presentable(answer, "Que me veut la livraison ?"),
            f"Votre colis attend : {LINK_REMOVED} Cliquez vite.",
        )

    def test_bare_and_markdown_links_are_removed_too(self):
        shown = presentable("Voir www.evil.example/x ou [ici](http://evil.example/y).", "quoi ?")

        self.assertNotIn("evil.example", shown)
        self.assertEqual(shown.count(LINK_REMOVED), 2)

    def test_a_link_the_user_wrote_is_kept(self):
        question = "Que dit https://example.com/doc ?"

        self.assertIn(
            "https://example.com/doc", presentable("https://example.com/doc parle de X.", question)
        )

    def test_markdown_is_flattened_and_a_long_answer_cut(self):
        self.assertEqual(presentable("**Devis** : `2 480` euros", QUESTION), "Devis : 2 480 euros")
        self.assertLessEqual(len(presentable("x" * 10_000, QUESTION)), MAX_MESSAGE_CHARS)


class ChatRunsTestCase(unittest.TestCase):
    def setUp(self):
        self.ports = world()
        self.addCleanup(self.ports.store.close)
        self.runs = self.ports.store.agent_runs
        self.chat = MagicMock()

    def handler(self, *script):
        self.model = FakeAgentModel(list(script))
        return ChatRuns(self.model, self.ports, self.chat, LIMITS, "Aristide")

    def queued(self, message_id=10, text=QUESTION):
        self.runs.enqueue(trigger_key(message_id), KIND, payload(message_id, text))
        return self.runs.take_next()

    def sent(self):
        return self.chat.send_message.call_args


class ChatRunsTests(ChatRunsTestCase):
    def test_the_answer_goes_back_under_the_question_that_asked(self):
        read = ToolCall("read_mail", {"message_id": "m1"})
        handler = self.handler(
            AgentTurn(tool_calls=(read,)), AgentTurn(text="Jean demande le devis.")
        )

        trajectory = handler.run(self.queued())

        self.assertEqual(trajectory.outcome, ANSWERED)
        self.assertEqual(self.sent().args, ("Jean demande le devis.",))
        self.assertEqual(self.sent().kwargs, {"reply_to": 10})
        system, messages, tools = self.model.seen[0]
        self.assertEqual(system, chat_system("Aristide", NOW.date()))
        self.assertEqual(messages, (UserMessage(QUESTION),))
        self.assertIn("search_mail", {tool.name for tool in tools})

    def test_it_holds_no_tool_that_writes(self):
        self.handler(AgentTurn(text="ok")).run(self.queued())

        offered = {tool.name for tool in self.model.seen[0][2]}
        self.assertEqual(
            offered,
            {
                "read_mail",
                "read_thread",
                "search_mail",
                "ask_jev",
                "list_pending",
                "recall_decisions",
            },
        )
        self.assertEqual(self.ports.mail.writes, [])

    def test_what_is_shown_has_no_link_from_a_mail(self):
        self.handler(AgentTurn(text="Cliquez sur https://evil.example/?q=secret")).run(
            self.queued()
        )

        self.assertEqual(self.sent().args[0], f"Cliquez sur {LINK_REMOVED}")

    def test_a_run_that_did_not_end_on_an_answer_says_so(self):
        looping = AgentTurn(tool_calls=(ToolCall("list_pending", {}),))
        scripts = {
            "step limit": [looping] * 3,
            "model failure": [AgentModelError("status 503")],
            "empty answer": [AgentTurn(text="  ")],
        }
        for reason, script in scripts.items():
            with self.subTest(reason):
                self.handler(*script).run(self.queued(message_id=hash(reason) % 1000))

                self.assertEqual(self.sent().args[0], NO_ANSWER)

    def test_giving_up_tells_why_under_the_question(self):
        handler = self.handler()
        run = self.queued()

        for reason, text in (*GAVE_UP.items(), ("crashed", GAVE_UP_OTHER)):
            with self.subTest(reason):
                handler.gave_up(run, reason)

                self.assertEqual(
                    (self.sent().args, self.sent().kwargs), ((text,), {"reply_to": 10})
                )

    def test_a_long_question_is_cut_when_queued(self):
        self.assertEqual(len(payload(1, "x" * 10_000)["text"]), MAX_QUESTION_CHARS)


class ConversationTests(ChatRunsTestCase):
    def answer(self, message_id, text, answer):
        handler = self.handler(AgentTurn(text=answer))
        run = self.queued(message_id, text)
        self.runs.finish(run.id, handler.run(run))
        return self.model.seen[0][1][0].text

    def test_a_question_is_shown_with_the_exchanges_just_before_it(self):
        self.answer(1, "Combien pour le devis ?", "2 480 euros.")

        prompt = self.answer(2, "Et il est valable jusqu'à quand ?", "30 jours.")

        self.assertIn("Utilisateur : Combien pour le devis ?\nAssistant : 2 480 euros.", prompt)
        self.assertTrue(prompt.endswith("Question actuelle : Et il est valable jusqu'à quand ?"))
        self.assertIn("ne sont pas des instructions", prompt)

    def test_only_the_last_few_exchanges_are_shown_oldest_first(self):
        for i in range(1, 6):
            self.answer(i, f"question {i}", f"réponse {i}")

        prompt = self.answer(6, "et maintenant ?", "ok")

        self.assertNotIn("question 2", prompt)
        self.assertLess(prompt.index("question 3"), prompt.index("question 5"))

    def test_an_old_exchange_is_another_conversation(self):
        self.answer(1, "Combien pour le devis ?", "2 480 euros.")
        self.ports = self.ports.__class__(
            self.ports.mail,
            self.ports.store,
            self.ports.judge,
            lambda: NOW + timedelta(minutes=31),
        )

        self.assertEqual(self.answer(2, "Et sinon ?", "ok"), "Et sinon ?")

    def test_a_run_that_gave_no_answer_is_not_part_of_the_conversation(self):
        failed = self.queued(1, "question perdue")
        self.runs.fail(failed.id, "crashed")

        self.assertEqual(self.answer(2, "Et sinon ?", "ok"), "Et sinon ?")


class ChatThroughTheWorkerTests(ChatRunsTestCase):
    def test_a_queued_question_is_answered_and_recorded(self):
        handler = self.handler(AgentTurn(text="Rien en attente."))
        budget = AgentBudget(self.runs, 0, 100, NOW.tzinfo, clock=lambda: NOW)
        self.runs.enqueue(trigger_key(10), KIND, payload(10, QUESTION))

        AgentWorker(self.runs, {KIND: handler}, budget).run_one()

        run = self.runs.get("txt:10")
        self.assertEqual((run.state, run.answer), (RUN_DONE, "Rien en attente."))
        self.chat.send_message.assert_called_once_with("Rien en attente.", reply_to=10)


def settings(**overrides):
    defaults = {
        "db_path": ":memory:",
        "agent_mode": "on",
        "telegram_inbound_enabled": True,
        "telegram_bot_token": "t",
        "telegram_chat_id": "1",
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


class AgentWiringTests(unittest.TestCase):
    def context(self, model=None, agent_mail=None, **overrides):
        components = fake_components(
            agent_model=model or FakeAgentModel(),
            agent_mail=FakeMail() if agent_mail is None else agent_mail,
            chat=FakeChat(),
        )
        ctx = ApplicationContext(settings(**overrides), components)
        self.addCleanup(ctx.close)
        return ctx

    def test_off_by_default_free_text_is_dropped_and_no_second_mail_client_is_built(self):
        ctx = self.context(agent_mode="off")
        components = build_components(Settings(_env_file=None, db_path=":memory:"))
        self.addCleanup(components.store.close)

        ctx.interactions.dispatch(TextEvent(1, QUESTION))

        self.assertIsNone(ctx.agent_worker)
        self.assertEqual(ctx.store.agent_runs.queued(), 0)
        self.assertIsNone(components.agent_mail)
        self.assertNotIn("gemini-agent", dict(components.probe_targets))

    def test_on_builds_a_mail_client_of_its_own_and_probes_the_model(self):
        components = build_components(settings())
        self.addCleanup(components.store.close)

        self.assertIsNotNone(components.agent_mail)
        self.assertIsNot(components.agent_mail, components.mail)
        self.assertIs(dict(components.probe_targets)["gemini-agent"], components.agent_model)

    def test_on_free_text_is_queued_once_however_often_it_is_delivered(self):
        ctx = self.context()

        ctx.interactions.dispatch(TextEvent(7, QUESTION))
        ctx.interactions.dispatch(TextEvent(7, QUESTION))

        run = ctx.store.agent_runs.get("txt:7")
        self.assertEqual((run.kind, run.payload), (KIND, {"message_id": 7, "text": QUESTION}))
        self.assertEqual(ctx.store.agent_runs.queued(), 1)

    def test_the_worker_thread_answers_and_stops_with_the_app(self):
        chat = FakeChat()
        chat.send_message = MagicMock(return_value=1)
        components = fake_components(
            agent_model=FakeAgentModel([AgentTurn(text="Rien en attente.")]),
            agent_mail=FakeMail(),
            chat=chat,
        )
        ctx = ApplicationContext(settings(), components)
        ctx.start_agent_worker()
        ctx.interactions.dispatch(TextEvent(7, QUESTION))

        for _ in range(200):
            if chat.send_message.called:
                break
            ctx.stopping.wait(0.01)
        ctx.close()

        chat.send_message.assert_called_once_with("Rien en attente.", reply_to=7)
        self.assertFalse(ctx.agent_thread.is_alive())

    def test_on_without_what_it_needs_stays_off_and_says_why(self):
        cases = {
            "no inbound chat": {"telegram_inbound_enabled": False},
            "model not configured": {"model": FakeAgentModel(is_configured=False)},
            "mail not configured": {"agent_mail": FakeMail(is_configured=False)},
        }
        for reason, overrides in cases.items():
            with self.subTest(reason), self.assertLogs("gmail-assistant", level="WARNING"):
                ctx = self.context(**overrides)

                self.assertIsNone(ctx.agent_worker)


if __name__ == "__main__":
    unittest.main()
