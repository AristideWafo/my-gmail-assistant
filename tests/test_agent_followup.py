import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock, patch

from main import ApplicationContext
from src.agent import profiles
from src.agent.followup import STALE, FollowUpRuns
from src.agent.loop import Limits
from src.agent.toolsets.followup import (
    KEPT,
    PLACEHOLDER,
    REASON_NEEDED,
    THREAD_MOVED,
    UNKNOWN_ADVICE,
    UNSEEN,
)
from src.config import Settings
from src.domain import (
    CLOSED,
    ENDED_BY_TOOL,
    RUN_DONE,
    RUN_QUEUED,
    AgentTurn,
    CommandEvent,
    ToolCall,
    Trajectory,
)
from src.followup import compose
from src.followup.compose import MAX_WAIT, ComposedFollowUps, is_composed, trigger_key
from src.followup.offers import format_advice, format_offer
from src.followup.refresh import FollowUpTracker
from src.followup.state import OFFERED
from src.interactions.pending import PendingCommand
from src.storage import SqliteDecisionStore
from src.storage.migrations import LATEST_VERSION, MIGRATIONS, schema_version
from tests.agent_helpers import SECRET, world
from tests.fakes import FakeAgentModel, FakeChat, FakeMail, fake_components
from tests.followup_helpers import message, snapshot
from tests.test_follow_up_offers import PARIS, THURSDAY, OfferTestCase

TEXT = "Bonjour,\n\nAvez-vous pu regarder le devis ?\n\nAristide"
LIMITS = Limits(max_steps=4, max_tokens=10_000)


def writing(text=TEXT, **extra):
    return AgentTurn(tool_calls=(ToolCall("write_follow_up", {"text": text, **extra}),))


class ComposedColumnsTests(unittest.TestCase):
    def test_version_10_database_is_upgraded_and_its_threads_have_no_composition(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "assistant.db")
        conn = sqlite3.connect(path)
        conn.executescript("".join(MIGRATIONS[:10]) + "PRAGMA user_version = 10;")
        conn.execute(
            "INSERT INTO threads (thread_id, history_id, state, updated_at) "
            "VALUES ('t1', '1', 'closed', '2026-10-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        store = SqliteDecisionStore(path)
        self.addCleanup(store.close)

        self.assertEqual(schema_version(store._conn), LATEST_VERSION)
        thread = store.threads.get("t1")
        self.assertEqual(
            (thread.composed_text, thread.composed_for, thread.composed_advice), ("", "", "")
        )


class ComposeTestCase(OfferTestCase):
    """A thread t1 whose follow-up is due, as the offer tests set it up."""

    def setUp(self):
        super().setUp()
        self.now = THURSDAY + timedelta(days=7)
        self.runs = self.store.agent_runs
        self.notified = []

    def composed(self, use_in_offers=True):
        return ComposedFollowUps(
            self.runs, lambda: self.notified.append(1), use_in_offers, clock=lambda: self.now
        )

    def write(self, text=TEXT, advice=""):
        self.store.threads.update(
            "t1",
            lambda t: replace(t, composed_text=text, composed_for="m1", composed_advice=advice),
        )


class ComposedFollowUpsTests(ComposeTestCase):
    def test_a_due_thread_is_asked_for_once_per_anchor(self):
        composed = self.composed()

        composed.request(self.thread())
        composed.request(self.thread())

        run = self.runs.get("followup:t1:m1")
        self.assertEqual(
            (run.kind, run.payload), ("followup", {"thread_id": "t1", "anchor_id": "m1"})
        )
        self.assertEqual((self.runs.queued(), len(self.notified)), (1, 1))

    def test_a_thread_already_written_for_is_not_asked_again(self):
        self.write()

        self.composed().request(self.thread())

        self.assertEqual(self.runs.queued(), 0)

    def test_a_text_written_for_an_earlier_anchor_is_not_this_ones(self):
        self.store.threads.update("t1", lambda t: replace(t, composed_text=TEXT, composed_for="m0"))

        self.assertFalse(is_composed(self.thread()))
        self.assertIsNone(self.composed().text_for(self.thread()))

    def test_an_offer_waits_while_the_text_is_being_written(self):
        composed = self.composed()

        self.assertTrue(composed.still_writing(self.thread()))
        self.assertEqual(self.runs.get(trigger_key(self.thread())).state, RUN_QUEUED)
        self.assertTrue(composed.still_writing(self.thread()))
        self.runs.take_next()
        self.assertTrue(composed.still_writing(self.thread()))

    def test_an_offer_stops_waiting_for_a_worker_that_does_not_answer(self):
        composed = self.composed()
        composed.request(self.thread())

        self.now += MAX_WAIT

        self.assertFalse(composed.still_writing(self.thread()))
        self.assertIsNone(composed.text_for(self.thread()))

    def test_a_run_that_ended_without_a_text_is_not_waited_for(self):
        composed = self.composed()
        composed.request(self.thread())
        self.runs.fail(self.runs.take_next().id, "crashed")

        self.assertFalse(composed.still_writing(self.thread()))

    def test_once_written_the_text_and_its_advice_are_given(self):
        self.write(advice="wait: absent jusqu'au 15")
        composed = self.composed()

        self.assertFalse(composed.still_writing(self.thread()))
        self.assertEqual(composed.text_for(self.thread()), TEXT)
        self.assertEqual(composed.advice_for(self.thread()), "wait: absent jusqu'au 15")

    def test_in_observation_the_text_is_written_but_never_given_to_offers(self):
        self.write(advice="drop: déjà répondu")
        composed = self.composed(use_in_offers=False)

        self.assertFalse(composed.still_writing(self.thread()))
        self.assertIsNone(composed.text_for(self.thread()))
        self.assertEqual(composed.advice_for(self.thread()), "")


class CompositionFollowsTheAnchorTests(ComposeTestCase):
    def test_a_refresh_keeps_the_text_for_the_same_anchor(self):
        self.write(advice="wait: absent")
        self.mail.threads[0] = replace(self.mail.threads[0], history_id="2")

        self.tracker.refresh()

        self.assertTrue(is_composed(self.thread()))
        self.assertEqual(self.thread().composed_advice, "wait: absent")

    def test_a_new_mail_of_mine_drops_the_text_written_for_the_previous_one(self):
        self.write()
        self.mail.threads[0] = snapshot(
            *self.mail.threads[0].messages, message("m5", day=9), history_id="2"
        )

        self.tracker.refresh()

        thread = self.thread()
        self.assertEqual(thread.anchor.message_id, "m5")
        self.assertEqual((thread.composed_text, thread.composed_for), ("", ""))


class TrackerTellsDueThreadsTests(ComposeTestCase):
    def make_tracker(self, on_due):
        return FollowUpTracker(
            self.mail, self.store, PARIS, 3, 50, clock=lambda: self.now, on_due=on_due
        )

    def test_each_refresh_tells_of_the_threads_a_follow_up_is_due_for(self):
        told = []

        self.make_tracker(lambda thread: told.append(thread.thread_id)).refresh()

        self.assertEqual(told, ["t1"])

    def test_a_thread_not_yet_due_is_not_told(self):
        self.now = THURSDAY - timedelta(days=30)
        told = []

        self.make_tracker(told.append).refresh()

        self.assertEqual(told, [])

    def test_a_failure_to_ask_does_not_stop_the_refresh(self):
        def boom(thread):
            raise RuntimeError("queue down")

        with self.assertLogs("src.followup.refresh", level="WARNING"):
            self.make_tracker(boom).refresh()


class OffersWithComposedTextTests(ComposeTestCase):
    def offers_with(self, composed):
        offers = self.make_offers()
        offers._composed = composed
        return offers

    def offered_text(self):
        return self.chat.send_message.call_args.args[0]

    def test_the_agents_text_is_offered_and_is_what_would_be_sent(self):
        self.write()

        self.assertEqual(self.offers_with(self.composed()).run(), 1)

        self.assertIn(TEXT, self.offered_text())
        self.assertEqual(self.thread().proposal_text, TEXT)

    def test_its_advice_is_shown_under_the_text(self):
        self.write(advice="wait: absent jusqu'au 15")

        self.offers_with(self.composed()).run()

        self.assertTrue(
            self.offered_text().endswith(
                "Avis de l'assistant : attendre encore — absent jusqu'au 15"
            )
        )

    def test_nothing_is_offered_while_the_text_is_being_written(self):
        offers = self.offers_with(self.composed())

        self.assertEqual(offers.run(), 0)

        self.chat.send_message.assert_not_called()
        self.assertEqual(self.runs.queued(), 1)

    def test_the_fixed_text_is_offered_when_the_agent_gave_none(self):
        composed = self.composed()
        composed.request(self.thread())
        self.runs.fail(self.runs.take_next().id, "over_budget")

        self.assertEqual(self.offers_with(composed).run(), 1)

        self.assertIn("Je me permets de revenir vers vous", self.offered_text())

    def test_in_observation_the_fixed_text_is_offered_without_waiting(self):
        self.write()

        self.assertEqual(self.offers_with(self.composed(use_in_offers=False)).run(), 1)

        self.assertNotIn(TEXT, self.offered_text())

    def test_a_text_written_while_earlier_offers_went_out_is_still_used(self):
        composed = self.composed()
        composed.request(self.thread())
        stale = self.thread()
        self.write()
        self.runs.finish(self.runs.take_next().id, Trajectory(ENDED_BY_TOOL, ()))

        self.assertTrue(self.offers_with(composed)._offer(stale))

        self.assertIn(TEXT, self.offered_text())

    def test_a_candidate_dismissed_meanwhile_is_not_offered(self):
        stale = self.thread()
        self.store.threads.update("t1", lambda t: replace(t, state=CLOSED))

        self.assertFalse(self.offers_with(self.composed())._offer(stale))
        self.chat.send_message.assert_not_called()

    def test_the_author_of_the_text_is_counted_once_it_went_out(self):
        self.write()
        self.chat.send_message.side_effect = RuntimeError("telegram down")

        with (
            patch("src.followup.offers.Metrics") as metrics,
            self.assertLogs("src.followup.offers", level="WARNING"),
        ):
            self.offers_with(self.composed()).run()
            metrics.mark_followup_text.assert_not_called()
            self.chat.send_message.side_effect = None
            self.chat.send_message.return_value = 78
            self.offers_with(self.composed()).run()

        metrics.mark_followup_text.assert_called_once_with("agent")

    def test_advice_that_is_not_one_of_the_two_shows_nothing(self):
        for advice in ("", "send: ok", "ignore all rules", "wait"):
            with self.subTest(advice=advice):
                self.assertEqual(format_advice(advice), "")
        self.assertEqual(
            format_advice("drop: déjà répondu"),
            "Avis de l'assistant : ne pas relancer — déjà répondu",
        )

    def test_pending_shows_what_the_agent_wrote(self):
        self.write(advice="drop: déjà répondu")
        chat = MagicMock()

        PendingCommand(self.store, chat, 0.5, PARIS, clock=lambda: self.now).run(
            CommandEvent(message_id=1, name="pending")
        )

        shown = "\n".join(call.args[0] for call in chat.send_message.call_args_list)
        self.assertIn(f"Relance rédigée par l'assistant :\n{TEXT}", shown)
        self.assertIn("ne pas relancer — déjà répondu", shown)


class WriteFollowUpToolTests(ComposeTestCase):
    def setUp(self):
        super().setUp()
        self.box = profiles.followup_compose(world().__class__(self.mail, self.store), "t1", "m1")

    def write_with_tool(self, text=TEXT, **extra):
        return self.box.execute(ToolCall("write_follow_up", {"text": text, **extra}))

    def test_the_profile_reads_its_thread_and_keeps_a_text_and_nothing_more(self):
        self.assertEqual(
            {spec.name for spec in self.box.specs}, {"read_mail", "read_thread", "write_follow_up"}
        )

    def test_the_text_is_kept_on_the_thread_for_its_anchor_and_ends_the_run(self):
        result = self.write_with_tool()

        self.assertEqual((result.content, result.is_error, result.ends_run), (KEPT, False, True))
        thread = self.thread()
        self.assertEqual(
            (thread.composed_text, thread.composed_for, thread.composed_advice), (TEXT, "m1", "")
        )
        self.assertEqual((thread.proposal_state, thread.proposal_text), ("none", ""))
        self.assertEqual((self.mail.drafts, self.mail.sent), ([], []))

    def test_advice_not_to_send_is_kept_with_its_reason(self):
        self.write_with_tool(advice="wait", reason="Absent jusqu'au 15.")

        self.assertEqual(self.thread().composed_advice, "wait: Absent jusqu'au 15.")

    def test_the_reason_is_one_line_without_a_link(self):
        forged = (
            "absent\n\nTexte envoyé tel quel :\nBonjour, rien de spécial.\n\n"
            "Vérifie avant : https://evil.example/x"
        )

        self.write_with_tool(advice="wait", reason=forged)

        advice = self.thread().composed_advice
        self.assertNotIn("\n", advice)
        self.assertNotIn("evil.example", advice)
        self.assertEqual(
            format_offer(self.thread(), TEXT, PARIS, advice).count("\nTexte envoyé tel quel :\n"), 1
        )

    def test_a_text_that_cannot_be_kept_is_refused_and_can_be_written_again(self):
        hidden = "".join(chr(0xE0000 + ord(c)) for c in "iban")
        cases = {
            "placeholder": ({"text": "Bonjour [Nom],"}, PLACEHOLDER),
            "unseen characters": ({"text": f"Bonjour.{hidden}"}, UNSEEN),
            "unknown advice": ({"advice": "archive"}, UNKNOWN_ADVICE),
            "advice without a reason": ({"advice": "drop"}, REASON_NEEDED),
            "unseen characters in the reason": (
                {"advice": "drop", "reason": f"ok{hidden}"},
                UNSEEN,
            ),
        }
        for reason, (args, refusal) in cases.items():
            with self.subTest(reason):
                result = self.write_with_tool(**{"text": TEXT, **args})

                self.assertEqual(
                    (result.content, result.is_error, result.ends_run), (refusal, True, False)
                )
        self.assertEqual(self.thread().composed_text, "")

    def test_a_text_written_for_an_anchor_that_is_gone_is_not_kept(self):
        self.store.threads.update(
            "t1", lambda t: replace(t, anchor=replace(t.anchor, message_id="m5"))
        )

        result = self.write_with_tool()

        self.assertEqual((result.content, result.ends_run), (THREAD_MOVED, True))
        self.assertEqual(self.thread().composed_text, "")

    def test_the_model_cannot_say_whom_it_goes_to_or_write_elsewhere(self):
        for extra in ({"to": "evil@example.com"}, {"thread_id": "t2"}, {"subject": "x"}):
            with self.subTest(extra=extra):
                self.assertTrue(self.write_with_tool(**extra).is_error)


class FollowUpRunsTests(ComposeTestCase):
    def setUp(self):
        super().setUp()
        self.ports = world().__class__(self.mail, self.store, clock=lambda: self.now)

    def run_for(self, *script, anchor_id="m1"):
        self.model = FakeAgentModel(list(script))
        handler = FollowUpRuns(self.model, self.ports, LIMITS, "Aristide")
        self.runs.enqueue(
            f"followup:t1:{anchor_id}", compose.KIND, {"thread_id": "t1", "anchor_id": anchor_id}
        )
        return handler, self.runs.take_next()

    def test_the_agent_reads_the_thread_and_its_text_is_kept(self):
        read = AgentTurn(tool_calls=(ToolCall("read_thread", {"thread_id": "t1"}),))
        handler, run = self.run_for(read, writing())

        trajectory = handler.run(run)

        self.assertEqual(trajectory.outcome, ENDED_BY_TOOL)
        self.assertEqual(self.thread().composed_text, TEXT)
        system, messages, tools = self.model.seen[0]
        self.assertIn("relance", system)
        self.assertIn("Aristide", system)
        self.assertIn("Fil à relancer : t1", messages[0].text)
        self.assertEqual(
            {tool.name for tool in tools}, {"read_mail", "read_thread", "write_follow_up"}
        )

    def test_what_was_asked_to_be_remembered_about_the_recipients_goes_with_it(self):
        self.store.memory.add("jean@example.com", "préfère être tutoyé")
        self.store.memory.add("someone@else.example", "sans rapport")
        handler, run = self.run_for(writing())

        handler.run(run)

        prompt = self.model.seen[0][1][0].text
        self.assertIn("- jean@example.com : préfère être tutoyé", prompt)
        self.assertNotIn("sans rapport", prompt)

    def test_a_thread_no_longer_waiting_or_with_another_anchor_is_not_written_for(self):
        stale = {
            "another anchor": lambda: self.run_for(writing(), anchor_id="m0"),
            "answered": lambda: (
                self.store.threads.update("t1", lambda t: replace(t, state=CLOSED)),
                self.run_for(writing()),
            )[1],
        }
        for reason, prepare in stale.items():
            with self.subTest(reason):
                handler, run = prepare()

                self.assertEqual(handler.run(run).outcome, STALE)
                self.assertEqual(self.model.seen, [])
                self.assertEqual(self.thread().composed_text, "")

    def test_with_offers_using_its_text_a_thread_already_offered_is_not_written_for(self):
        self.store.threads.update("t1", lambda t: replace(t, proposal_state=OFFERED))
        handler, run = self.run_for(writing())
        handler._offers_use_it = True

        self.assertEqual(handler.run(run).outcome, STALE)
        self.assertEqual(self.thread().composed_text, "")

    def test_in_observation_a_thread_already_offered_is_still_written_for(self):
        self.store.threads.update("t1", lambda t: replace(t, proposal_state=OFFERED))
        handler, run = self.run_for(writing())

        handler.run(run)

        self.assertEqual(self.thread().composed_text, TEXT)

    def test_giving_up_is_silent(self):
        handler, run = self.run_for()

        with self.assertLogs("src.agent.followup", level="INFO"):
            handler.gave_up(run, "over_budget")

        self.chat.send_message.assert_not_called()

    def test_another_thread_stays_out_of_reach(self):
        self.mail.threads.extend(world().mail.threads[1:])
        self.mail.unread = world().mail.unread
        elsewhere = AgentTurn(tool_calls=(ToolCall("read_mail", {"message_id": "m9"}),))
        handler, run = self.run_for(elsewhere, writing())

        trajectory = handler.run(run)

        (refused,) = trajectory.messages[2].results
        self.assertTrue(refused.is_error)
        self.assertNotIn(SECRET, refused.content)


class FollowUpWiringTests(unittest.TestCase):
    def context(self, **overrides):
        defaults = {
            "db_path": ":memory:",
            "agent_mode": "on",
            "follow_up_mode": "on",
            "telegram_inbound_enabled": True,
            "telegram_bot_token": "t",
            "telegram_chat_id": "1",
        }
        settings = Settings(_env_file=None, **{**defaults, **overrides})
        components = fake_components(
            agent_model=FakeAgentModel(), agent_mail=FakeMail(), chat=FakeChat()
        )
        ctx = ApplicationContext(settings, components)
        self.addCleanup(ctx.close)
        return ctx

    def test_off_by_default_follow_ups_keep_the_fixed_text(self):
        ctx = self.context()

        self.assertIsNone(ctx.composed_follow_ups)
        self.assertIsNone(ctx.follow_up_offers._composed)
        self.assertNotIn(compose.KIND, ctx.agent_worker._handlers)

    def test_shadow_writes_without_changing_offers_and_on_uses_the_text(self):
        for mode, used in (("shadow", False), ("on", True)):
            with self.subTest(mode=mode):
                ctx = self.context(agent_follow_up_mode=mode)

                self.assertIs(ctx.follow_up_offers._composed, ctx.composed_follow_ups)
                self.assertEqual(ctx.composed_follow_ups._use_in_offers, used)
                self.assertIn(compose.KIND, ctx.agent_worker._handlers)
                self.assertIsNotNone(ctx.follow_ups._on_due)

    def test_without_the_agent_it_says_so_and_keeps_the_fixed_text(self):
        with self.assertLogs("gmail-assistant", level="WARNING"):
            ctx = self.context(agent_follow_up_mode="on", agent_mode="off")

        self.assertIsNone(ctx.composed_follow_ups)

    def test_a_written_follow_up_goes_through_the_worker(self):
        ctx = self.context(agent_follow_up_mode="on")
        ctx.store.agent_runs.enqueue(
            "followup:t1:m1", compose.KIND, {"thread_id": "t1", "anchor_id": "m1"}
        )

        ctx.agent_worker.run_one()

        self.assertEqual(ctx.store.agent_runs.get("followup:t1:m1").state, RUN_DONE)


if __name__ == "__main__":
    unittest.main()
