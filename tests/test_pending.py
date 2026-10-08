import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.domain import (
    CLOSED,
    WAITING_FOR_THEM,
    CallbackEvent,
    CommandEvent,
    FollowUpAnchor,
    TrackedThread,
)
from src.followup.state import NOT_USEFUL, USEFUL, would_propose
from src.interactions import InteractionHandler
from src.interactions.callbacks import Callback, follow_up_buttons, parse_callback
from src.interactions.pending import NOTHING_PENDING, PendingCommand, format_header, format_thread
from src.storage import SqliteDecisionStore

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


def waiting(thread_id="t1", due_days=-1, expects=0.9, to=("jean@example.com",), cc=(), **fields):
    anchor = FollowUpAnchor(
        message_id=f"m-{thread_id}",
        sent_at=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
        to=tuple(to),
        cc=tuple(cc),
        subject="Devis cuisine",
    )
    return TrackedThread(
        thread_id=thread_id,
        history_id="1",
        state=WAITING_FOR_THEM,
        updated_at=NOW,
        anchor=anchor,
        due_at=NOW + timedelta(days=due_days),
        expects_answer=expects,
        jev_asked_for=anchor.message_id,
        **fields,
    )


class FormatTests(unittest.TestCase):
    def test_a_late_thread_judged_to_wait_says_a_follow_up_would_be_offered(self):
        text = format_thread(waiting(cc=("paul@example.com",)), 0.5, NOW, PARIS, 1, 2)

        self.assertEqual(
            text.splitlines(),
            [
                "⏳ En attente 1/2",
                "À : jean@example.com (+1)",
                "Objet : Devis cuisine",
                "Envoyé le 05/10, relance due depuis le 08/10",
                "JEV : attend une réponse (90%)",
                "Une relance serait proposée.",
            ],
        )

    def test_other_states_of_a_thread(self):
        cases = {
            "not due": (waiting(due_days=2), "relance prévue le 11/10"),
            "judged no": (waiting(expects=0.2), "ne semble rien attendre (20%)"),
            "not judged": (waiting(expects=None), "pas encore évalué"),
            "rated": (waiting(verdict=USEFUL), "Ton avis : relance utile"),
        }
        for name, (thread, expected) in cases.items():
            with self.subTest(name):
                text = format_thread(thread, 0.5, NOW, PARIS, 1, 1)
                self.assertIn(expected, text)
                if name != "rated":
                    self.assertNotIn("serait proposée", text)

    def test_the_header_counts_the_verdicts_on_mails_judged_to_wait(self):
        rated = [waiting(verdict=USEFUL), waiting(verdict=NOT_USEFUL),
                 waiting(expects=0.1, verdict=USEFUL)]

        header = format_header(12, 10, rated, 0.5)

        self.assertIn("12 mail(s) envoyé(s) en attente de réponse, les 10 plus urgents", header)
        self.assertIn("Relances jugées utiles : 1 sur 2", header)

    def test_the_header_is_short_without_verdicts(self):
        self.assertEqual(format_header(1, 1, [], 0.5), "⏳ 1 mail(s) envoyé(s) en attente de réponse")


class PendingCommandTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.chat = MagicMock()
        self.command = PendingCommand(self.store, self.chat, 0.5, PARIS, clock=lambda: NOW)

    def run_command(self):
        self.command.run(CommandEvent(message_id=7, name="pending"))
        return self.chat.send_message.call_args_list

    def test_nothing_waiting_is_said_plainly(self):
        self.store.threads.save(replace(waiting(), state=CLOSED))

        calls = self.run_command()

        self.assertEqual(calls[0].args[0], NOTHING_PENDING)
        self.assertEqual(calls[0].kwargs["reply_to"], 7)

    def test_threads_come_earliest_due_first_with_buttons_until_rated(self):
        self.store.threads.save(waiting("late", due_days=-3))
        self.store.threads.save(waiting("soon", due_days=1))
        self.store.threads.save(waiting("rated", due_days=-1, verdict=USEFUL))

        calls = self.run_command()

        header, *items = calls
        self.assertIn("3 mail(s)", header.args[0])
        self.assertEqual([c.kwargs["buttons"] for c in items],
                         [follow_up_buttons("late"), None, follow_up_buttons("soon")])

    def test_at_most_ten_are_listed(self):
        for i in range(12):
            self.store.threads.save(waiting(f"t{i:02}", due_days=-i))

        calls = self.run_command()

        self.assertEqual(len(calls), 11)


class FollowUpCallbackTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.threads.save(waiting())
        self.bot = MagicMock()
        self.handler = InteractionHandler(self.store, self.bot, MagicMock(), follow_up_threshold=0.5)
        metrics = patch("src.interactions.handlers.Metrics")
        self.metrics = metrics.start()
        self.addCleanup(metrics.stop)

    def press(self, data):
        self.handler.dispatch(CallbackEvent(callback_id="cb", message_id=42, data=data))
        return self.bot.answer_callback.call_args.args[1]

    def test_buttons_carry_the_thread_and_parse_back(self):
        (useful, unwanted), = follow_up_buttons("t1")

        self.assertEqual(parse_callback(useful[1]), Callback("fu", "t1", USEFUL))
        self.assertEqual(parse_callback(unwanted[1]), Callback("fu", "t1", NOT_USEFUL))
        self.assertIsNone(parse_callback("fu:v:t1"))
        self.assertIsNone(parse_callback("fu:u:"))

    def test_a_verdict_is_stored_counted_and_its_buttons_cleared(self):
        answer = self.press("fu:n:t1")

        self.assertEqual(answer, "Noté : pas de relance")
        self.assertEqual(self.store.threads.get("t1").verdict, NOT_USEFUL)
        self.metrics.mark_followup_verdict.assert_called_once_with(NOT_USEFUL)
        self.bot.clear_buttons.assert_called_once_with(42)

    def test_an_unwanted_follow_up_is_never_proposed(self):
        self.press("fu:n:t1")

        self.assertFalse(would_propose(self.store.threads.get("t1"), 0.5, NOW))

    def test_an_unknown_thread_is_reported_and_nothing_stored(self):
        answer = self.press("fu:u:nope")

        self.assertEqual(answer, "Fil inconnu")
        self.assertIsNone(self.store.threads.get("nope"))
        self.bot.clear_buttons.assert_not_called()

    def test_the_command_exists_only_when_threads_are_tracked(self):
        self.assertIn("pending", self.handler.commands.help_text())
        without = InteractionHandler(self.store, self.bot, MagicMock())
        self.assertNotIn("pending", without.commands.help_text())

    def test_rated_threads_are_listed_by_the_store(self):
        self.press("fu:u:t1")

        self.assertEqual([t.thread_id for t in self.store.threads.rated()], ["t1"])


if __name__ == "__main__":
    unittest.main()
