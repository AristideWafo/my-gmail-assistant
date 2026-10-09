import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from src.agent import profiles
from src.agent.chat import KIND, ChatRuns, payload, trigger_key
from src.agent.loop import Limits
from src.agent.reply import (
    MAX_PREVIEW_CHARS,
    PROPOSAL_LIFETIME,
    SEND_REPLY,
    format_proposal,
    reply_target,
)
from src.agent.toolsets.proposals import NO_ONE_TO_ANSWER, PLACEHOLDER, PROPOSED, TOO_LONG
from src.domain import (
    ACTION_CANCELLED,
    ACTION_DONE,
    ACTION_FAILED,
    ACTION_PENDING,
    ENDED_BY_TOOL,
    AgentTurn,
    CallbackEvent,
    ReplyEvent,
    ToolCall,
)
from src.interactions import InteractionHandler
from src.interactions import proposals as actions_module
from src.interactions.callbacks import parse_callback, proposal_buttons
from tests.agent_helpers import NOW, world
from tests.fakes import FakeAgentModel
from tests.followup_helpers import message, snapshot

BODY = "Bonjour Jean,\n\nLe devis me convient.\n\nAristide"
JEAN = message("m1", sender="jean@example.com", to=("me@example.com",))
MINE = message("m2", to=("jean@example.com", "paul@example.com"))


class ReplyTargetTests(unittest.TestCase):
    def test_their_last_message_is_the_one_answered(self):
        late = message("m3", sender="paul@example.com", subject="Re: Devis")

        target = reply_target(snapshot(JEAN, MINE, late))

        self.assertEqual(target.to, ("paul@example.com",))
        self.assertEqual((target.subject, target.in_reply_to), ("Re: Devis", "<m3@x>"))
        self.assertEqual(target.references, ("<m1@x>", "<m2@x>", "<m3@x>"))
        self.assertEqual(target.last_message_id, "m3")

    def test_my_mail_is_continued_to_those_i_wrote_to(self):
        target = reply_target(snapshot(MINE))

        self.assertEqual(target.to, ("jean@example.com", "paul@example.com"))
        self.assertEqual(target.in_reply_to, "<m2@x>")

    def test_an_automated_message_is_never_what_is_answered(self):
        absent = message("m3", sender="jean@example.com", automated=True)
        bounce = message("m4", sender="mailer-daemon@example.com", bounce=True)

        target = reply_target(snapshot(JEAN, MINE, absent, bounce))

        self.assertEqual((target.to, target.in_reply_to), (("jean@example.com",), "<m1@x>"))
        self.assertEqual(target.last_message_id, "m4")

    def test_a_thread_with_nobody_to_answer_has_no_target(self):
        cases = {
            "empty": snapshot(),
            "only automated": snapshot(message("m1", sender="x@example.com", automated=True)),
            "no-reply sender": snapshot(message("m1", sender="no-reply@shop.example")),
            "my mail to nobody": snapshot(message("m1", to=())),
        }
        for reason, thread in cases.items():
            with self.subTest(reason):
                self.assertIsNone(reply_target(thread))

    def test_what_is_not_one_plain_address_is_never_a_recipient(self):
        for sender in (
            "jean@example.com\nBcc: evil@example.com",
            "jean@example.com, evil@example.com",
            "Jean <jean@example.com>",
            "jean",
            "",
        ):
            with self.subTest(sender=sender):
                self.assertIsNone(reply_target(snapshot(message("m1", sender=sender))))


class ProposalTestCase(unittest.TestCase):
    def setUp(self):
        self.ports = world()
        self.addCleanup(self.ports.store.close)
        self.mail = self.ports.mail
        self.actions = self.ports.store.pending_actions
        self.chat = MagicMock()
        self.chat.send_message.return_value = 500
        self.box = profiles.chat_propose(self.ports, self.chat, reply_to=10)

    def propose(self, thread_id="t1", body=BODY, **extra):
        return self.box.execute(
            ToolCall("propose_reply", {"thread_id": thread_id, "body": body, **extra})
        )

    def proposal(self):
        return self.actions.pending_on(500)


class ProposeReplyTests(ProposalTestCase):
    def test_the_profile_is_chat_read_plus_the_proposal(self):
        read_only = {spec.name for spec in profiles.chat_read(self.ports).specs}

        self.assertEqual({spec.name for spec in self.box.specs}, read_only | {"propose_reply"})

    def test_it_stores_and_shows_the_reply_and_writes_nothing_to_the_mailbox(self):
        result = self.propose()

        self.assertEqual(
            (result.content, result.is_error, result.ends_run), (PROPOSED, False, True)
        )
        action = self.proposal()
        self.assertEqual((action.kind, action.state), (SEND_REPLY, ACTION_PENDING))
        self.assertEqual(action.expires_at, NOW + PROPOSAL_LIFETIME)
        self.assertEqual(
            action.payload,
            {
                "thread_id": "t1",
                "to": ["jean@example.com"],
                "subject": "Devis",
                "body": BODY,
                "in_reply_to": "<m1@x>",
                "references": ["<m1@x>", "<m2@x>"],
                "last_message_id": "m2",
            },
        )
        shown = self.chat.send_message.call_args
        self.assertEqual(shown.args[0], format_proposal(action.payload))
        self.assertIn(BODY, shown.args[0])
        self.assertEqual(shown.kwargs, {"buttons": proposal_buttons(action.id), "reply_to": 10})
        self.assertEqual(self.mail.writes, [])

    def test_the_model_cannot_name_a_recipient(self):
        for extra in ({"to": "evil@example.com"}, {"cc": "evil@example.com"}, {"subject": "x"}):
            with self.subTest(extra=extra):
                result = self.propose(**extra)

                self.assertTrue(result.is_error)
                self.assertFalse(result.ends_run)
        self.chat.send_message.assert_not_called()

    def test_a_refused_proposal_shows_nothing_and_lets_the_model_try_again(self):
        cases = {
            "unknown thread": ({"thread_id": "nope"}, NO_ONE_TO_ANSWER),
            "placeholder left": ({"body": "Bonjour [Nom], d'accord."}, PLACEHOLDER),
            "nothing but a preamble": ({"body": "Voici la réponse :"}, PLACEHOLDER),
        }
        for reason, (args, refusal) in cases.items():
            with self.subTest(reason):
                result = self.propose(**args)

                self.assertEqual(
                    (result.content, result.is_error, result.ends_run), (refusal, True, False)
                )
        self.chat.send_message.assert_not_called()
        self.assertEqual(self.mail.writes, [])

    def test_a_thread_of_automated_senders_cannot_be_answered(self):
        self.mail.threads.append(
            snapshot(message("n1", sender="no-reply@shop.example"), thread_id="t5")
        )

        self.assertEqual(self.propose(thread_id="t5").content, NO_ONE_TO_ANSWER)

    def test_a_reply_that_cannot_be_shown_whole_is_refused(self):
        self.mail.threads[0] = snapshot(
            message("m1", sender="jean@example.com", subject="s" * 2000)
        )

        result = self.propose(body="x" * 2900)

        self.assertEqual(
            (result.content, result.is_error, result.ends_run), (TOO_LONG, True, False)
        )
        self.chat.send_message.assert_not_called()

    def test_the_longest_reply_accepted_still_fits_in_one_message(self):
        self.propose(body="x" * 3000)

        self.assertLessEqual(len(self.chat.send_message.call_args.args[0]), MAX_PREVIEW_CHARS)

    def test_the_preamble_a_model_adds_is_not_part_of_the_reply(self):
        self.propose(body=f"Voici une proposition de réponse :\n{BODY}")

        self.assertEqual(self.proposal().payload["body"], BODY)

    def test_a_proposal_that_could_not_be_shown_can_never_be_sent(self):
        self.chat.send_message.side_effect = RuntimeError("telegram down")

        with self.assertRaises(RuntimeError):
            self.propose()

        (stored,) = self.ports.store._conn.execute("SELECT id FROM pending_actions").fetchall()
        self.assertIsNone(self.actions.begin(stored["id"], 500))

    def test_a_revision_withdraws_the_proposal_it_replaces(self):
        self.propose()
        first = self.proposal()
        self.chat.send_message.return_value = 501
        box = profiles.chat_propose(self.ports, self.chat, 11, replaces=(first.id, 500))

        box.execute(ToolCall("propose_reply", {"thread_id": "t1", "body": "Plus court."}))

        self.assertEqual(self.actions.get(first.id).state, ACTION_CANCELLED)
        self.chat.clear_buttons.assert_called_once_with(500)
        self.assertEqual(self.actions.pending_on(501).payload["body"], "Plus court.")

    def test_a_refused_revision_leaves_the_first_proposal_standing(self):
        self.propose()
        first = self.proposal()
        box = profiles.chat_propose(self.ports, self.chat, 11, replaces=(first.id, 500))

        box.execute(ToolCall("propose_reply", {"thread_id": "t1", "body": "[à compléter]"}))

        self.assertEqual(self.actions.get(first.id).state, ACTION_PENDING)
        self.chat.clear_buttons.assert_not_called()


class ProposalButtonsTests(ProposalTestCase):
    def setUp(self):
        super().setUp()
        self.propose()
        self.action = self.proposal()
        self.threads = {thread.thread_id: thread for thread in self.mail.threads}
        # The listener's own mail client, not the one the run read with.
        self.gmail = MagicMock()
        self.gmail.thread_snapshot.side_effect = self.threads.get
        self.gmail.create_draft.return_value = "draft-1"
        self.gmail.send_draft.return_value = True
        self.answers, self.cleared = [], []
        self.buttons = actions_module.ProposalActions(
            self.ports.store,
            self.gmail,
            lambda event, text: self.answers.append(text),
            self.cleared.append,
        )
        metrics = patch("src.interactions.proposals.Metrics")
        self.metrics = metrics.start()
        self.addCleanup(metrics.stop)

    def press(self, code="s", message_id=500, action_id=None):
        data = f"pa:{code}:{action_id or self.action.id}"
        self.buttons.handle(CallbackEvent("cb", message_id, data), parse_callback(data))
        return self.answers[-1]

    def state(self):
        return self.actions.get(self.action.id).state

    def test_send_creates_the_draft_from_what_was_stored_and_sends_it(self):
        self.assertEqual(self.press(), actions_module.SENT)

        self.gmail.create_draft.assert_called_once_with(
            "t1",
            "jean@example.com",
            "Devis",
            BODY,
            in_reply_to="<m1@x>",
            references=["<m1@x>", "<m2@x>"],
        )
        self.gmail.send_draft.assert_called_once_with("draft-1")
        self.assertEqual((self.state(), self.cleared), (ACTION_DONE, [500]))
        self.metrics.mark_agent_proposal.assert_called_once_with("sent")

    def test_a_second_or_redelivered_press_sends_nothing_more(self):
        self.press()

        self.assertEqual(self.press(), actions_module.UNKNOWN_PROPOSAL)
        self.gmail.send_draft.assert_called_once()

    def test_only_the_message_that_showed_the_proposal_is_honoured(self):
        self.assertEqual(self.press(message_id=501), actions_module.UNKNOWN_PROPOSAL)
        self.assertEqual(self.press(action_id="forged"), actions_module.UNKNOWN_PROPOSAL)
        self.gmail.create_draft.assert_not_called()
        self.assertEqual(self.state(), ACTION_PENDING)

    def test_an_expired_proposal_is_not_sent(self):
        self.ports.store._clock = lambda: NOW + PROPOSAL_LIFETIME + timedelta(minutes=1)
        self.actions._clock = self.ports.store._clock

        self.assertEqual(self.press(), actions_module.UNKNOWN_PROPOSAL)
        self.gmail.create_draft.assert_not_called()

    def test_a_thread_that_received_a_message_since_cancels_the_proposal(self):
        self.threads["t1"] = snapshot(
            *self.threads["t1"].messages, message("m3", sender="jean@example.com")
        )

        self.assertEqual(self.press(), actions_module.THREAD_CHANGED)
        self.gmail.create_draft.assert_not_called()
        self.assertEqual((self.state(), self.cleared), (ACTION_CANCELLED, [500]))

    def test_a_deleted_thread_cancels_the_proposal(self):
        self.threads.clear()

        self.assertEqual(self.press(), actions_module.THREAD_CHANGED)
        self.gmail.create_draft.assert_not_called()

    def test_gmail_being_unreachable_leaves_the_proposal_pressable(self):
        self.gmail.thread_snapshot.side_effect = RuntimeError("ssl")

        with self.assertLogs("src.interactions.proposals", level="WARNING"):
            self.assertEqual(self.press(), actions_module.CANNOT_VERIFY)

        self.assertEqual((self.state(), self.cleared), (ACTION_PENDING, []))
        self.gmail.create_draft.assert_not_called()

    def test_a_draft_that_cannot_be_created_sends_nothing(self):
        for failure in ({"return_value": None}, {"side_effect": RuntimeError("quota")}):
            with self.subTest(failure=failure):
                self.setUp()
                self.gmail.create_draft.configure_mock(**failure)

                self.assertEqual(self.press(), actions_module.DRAFT_FAILED)
                self.gmail.send_draft.assert_not_called()
                self.assertEqual(self.state(), ACTION_FAILED)

    def test_an_uncertain_send_is_said_and_never_retried(self):
        self.gmail.send_draft.side_effect = RuntimeError("timeout")

        with self.assertLogs("src.interactions.proposals", level="ERROR"):
            self.assertEqual(self.press(), actions_module.SEND_FAILED)

        self.assertEqual(self.state(), ACTION_FAILED)
        self.assertEqual(self.press(), actions_module.UNKNOWN_PROPOSAL)
        self.gmail.send_draft.assert_called_once()

    def test_cancel_sends_nothing_and_closes_the_proposal(self):
        self.assertEqual(self.press("c"), actions_module.CANCELLED)
        self.assertEqual((self.state(), self.cleared), (ACTION_CANCELLED, [500]))
        self.assertEqual(self.press("s"), actions_module.UNKNOWN_PROPOSAL)
        self.gmail.create_draft.assert_not_called()

    def test_an_action_of_another_kind_is_never_sent_as_a_reply(self):
        other = self.actions.propose("delete_thread", {"thread_id": "t1"}, PROPOSAL_LIFETIME)
        self.actions.attach_chat_message(other.id, 600)

        with self.assertLogs("src.interactions.proposals", level="WARNING"):
            answer = self.press(message_id=600, action_id=other.id)

        self.assertEqual(answer, actions_module.UNKNOWN_PROPOSAL)
        self.gmail.create_draft.assert_not_called()
        self.assertEqual(self.actions.get(other.id).state, ACTION_FAILED)


class ProposalCallbackTests(unittest.TestCase):
    def test_buttons_carry_the_action_and_parse_back(self):
        ((send, cancel),) = proposal_buttons("a1b2c3d4e5f60718")

        self.assertEqual(send, ("Envoyer", "pa:s:a1b2c3d4e5f60718"))
        self.assertEqual(parse_callback(send[1]).verdict, "send")
        self.assertEqual(parse_callback(cancel[1]).verdict, "cancel")
        self.assertEqual(parse_callback(send[1]).target, "a1b2c3d4e5f60718")

    def test_an_unknown_code_or_a_missing_id_is_refused(self):
        for data in ("pa:x:a1", "pa:s:", "pa:s"):
            with self.subTest(data=data):
                self.assertIsNone(parse_callback(data))


class ChatProposalRunTests(ProposalTestCase):
    def handler(self, *script, may_propose=True):
        self.model = FakeAgentModel(list(script))
        return ChatRuns(
            self.model, self.ports, self.chat, Limits(4, 10_000), "Aristide", may_propose
        )

    def queued(self, message_id, text, revises=None):
        runs = self.ports.store.agent_runs
        runs.enqueue(trigger_key(message_id), KIND, payload(message_id, text, revises))
        return runs.take_next()

    def proposing(self, body=BODY):
        return AgentTurn(tool_calls=(ToolCall("propose_reply", {"thread_id": "t1", "body": body}),))

    def test_a_proposal_ends_the_run_and_is_the_only_message(self):
        handler = self.handler(self.proposing(), AgentTurn(text="never said"))

        trajectory = handler.run(self.queued(10, "Réponds à Jean que le devis me va."))

        self.assertEqual(trajectory.outcome, ENDED_BY_TOOL)
        self.chat.send_message.assert_called_once()
        self.assertIn("Réponse proposée", self.chat.send_message.call_args.args[0])
        system, _, tools = self.model.seen[0]
        self.assertIn("propose_reply", system)
        self.assertNotIn("lecture seule", system)
        self.assertIn("propose_reply", {tool.name for tool in tools})

    def test_without_the_setting_the_run_cannot_propose(self):
        handler = self.handler(
            self.proposing(), AgentTurn(text="Je ne peux pas."), may_propose=False
        )

        handler.run(self.queued(10, "Réponds à Jean."))

        system, _, tools = self.model.seen[0]
        self.assertIn("lecture seule", system)
        self.assertNotIn("propose_reply", {tool.name for tool in tools})
        self.assertIsNone(self.proposal())
        self.chat.send_message.assert_called_once_with("Je ne peux pas.", reply_to=10)

    def test_a_reply_to_a_proposal_rewrites_it_and_withdraws_the_first(self):
        self.handler(self.proposing()).run(self.queued(10, "Réponds à Jean que le devis me va."))
        first = self.proposal()
        self.chat.send_message.return_value = 501

        self.handler(self.proposing("D'accord pour le devis.")).run(
            self.queued(11, "plus court", revises=first)
        )

        prompt = self.model.seen[0][1][0].text
        self.assertIn(BODY, prompt)
        self.assertIn("plus court", prompt)
        self.assertIn("fil t1", prompt)
        self.assertEqual(self.actions.get(first.id).state, ACTION_CANCELLED)
        self.assertEqual(self.actions.pending_on(501).payload["body"], "D'accord pour le devis.")


class RevisionRoutingTests(ProposalTestCase):
    def handler(self, on_revision):
        return InteractionHandler(self.ports.store, self.chat, self.mail, on_revision=on_revision)

    def test_a_reply_to_a_pending_proposal_is_a_request_to_rewrite_it(self):
        self.propose()
        asked = []

        self.handler(lambda event, action: asked.append((event.text, action.id))).dispatch(
            ReplyEvent(11, 500, "plus court")
        )

        self.assertEqual(asked, [("plus court", self.proposal().id)])
        self.assertEqual(self.mail.writes, [])

    def test_a_reply_to_a_closed_proposal_or_without_the_agent_is_not(self):
        self.propose()
        asked = []

        self.handler(None).dispatch(ReplyEvent(11, 500, "plus court"))
        self.actions.cancel(self.actions.pending_on(500).id, 500)
        self.handler(lambda event, action: asked.append(action)).dispatch(
            ReplyEvent(12, 500, "plus court")
        )

        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
