import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src.domain import CallbackEvent, CommandEvent, EmailMessage, TriageResult
from src.interactions import InteractionHandler
from src.interactions.callbacks import Callback, parse_callback, put_forward_buttons
from src.interactions.put_forward import (
    DAILY_ITEMS,
    LAST_SENT_KEY,
    NOTHING_PENDING,
    format_header,
    format_item,
)
from src.storage import SqliteDecisionStore
from tests.fakes import FakeMail


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)

    def record(self, **signals):
        email = EmailMessage("m1", "t", "Eaux <avis@eaux.example>", "Coupure mardi", "s", "  Eau\ncoupée ")
        triage = TriageResult("low", "notification_systeme", 0.9, "jev", 0.1, signals)
        self.store.record_decision(email, triage, "label", put_forward=True)
        return self.store.get("m1")

    def test_item_shows_position_sender_subject_reasons_and_a_flattened_excerpt(self):
        text = format_item(self.record(), ("service_change", "personal_deadline"), 1, 3)

        self.assertEqual(
            text,
            "👀 À voir 1/3\n"
            "De : Eaux <avis@eaux.example>\n"
            "Objet : Coupure mardi\n"
            "Pourquoi : coupure ou changement annoncé, à faire avant une date\n\n"
            "Eau coupée",
        )

    def test_item_without_a_known_reason_has_no_reason_line(self):
        self.assertNotIn("Pourquoi", format_item(self.record(), (), 1, 1))

    def test_header_counts_new_hidden_and_older_mails(self):
        self.assertEqual(format_header(1, 0, 0, True), "👀 À voir : 1 nouveau mail")
        self.assertEqual(format_header(3, 0, 0, True), "👀 À voir : 3 nouveaux mails")
        self.assertEqual(
            format_header(5, 2, 1, True),
            "👀 À voir : 7 nouveaux mails · 2 reporté(s) à demain · 1 plus ancien(s) en attente "
            "· /avoir pour tout voir",
        )

    def test_header_does_not_name_a_command_nobody_listens_to(self):
        self.assertNotIn("/avoir", format_header(5, 2, 1, False))


class ButtonsTests(unittest.TestCase):
    def test_buttons_acknowledge_or_reject_the_mail(self):
        parsed = [parse_callback(data) for _, data in put_forward_buttons("m1")[0]]

        self.assertEqual(
            parsed, [Callback("pf", "m1", "valid"), Callback("pf", "m1", "false_important")]
        )
        self.assertIsNone(put_forward_buttons("x" * 60))

    def test_a_button_set_only_accepts_the_verdicts_it_offers(self):
        for forged in ("pf:m:m1", "pf:u:m1", "fb:n:m1", "fb:m:m1", "rv:n:m1", "rv:u:m1", "pf:v:"):
            with self.subTest(forged=forged):
                self.assertIsNone(parse_callback(forged))
        self.assertEqual(parse_callback("rv:i:m1"), Callback("rv", "m1", "missed_important"))
        self.assertEqual(parse_callback("fb:u:m1"), Callback("fb", "m1", "false_urgent"))


class PutForwardListTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.chat = MagicMock()
        self.mail = FakeMail()
        self.handler = InteractionHandler(
            self.store, self.chat, self.mail, attention_threshold=0.5
        )
        self.list = self.handler.put_forward

    def decide(self, message_id, put_forward=True, route="label"):
        email = EmailMessage(message_id, "t", "a@b.example", f"subject {message_id}", "s", "body")
        triage = TriageResult(
            "low", "notification_systeme", 0.9, "jev", 0.1, {"service_change": 0.99}
        )
        self.store.record_decision(email, triage, route, put_forward)
        self.now += timedelta(minutes=1)

    def command(self, message_id=10):
        self.handler.dispatch(CommandEvent(message_id=message_id, name="avoir"))

    def sent(self):
        return self.chat.send_message.call_args_list

    def subjects(self):
        return [
            call.args[0].split("Objet : ")[1].split("\n")[0]
            for call in self.sent()
            if "Objet : " in call.args[0]
        ]

    def test_command_lists_pending_mails_oldest_first_with_buttons_and_sound(self):
        self.decide("first")
        self.decide("plain", put_forward=False)
        self.decide("second")

        self.command()

        self.assertEqual(self.subjects(), ["subject first", "subject second"])
        first = self.sent()[0]
        self.assertIn("À voir 1/2", first.args[0])
        self.assertIn("Pourquoi : coupure ou changement annoncé", first.args[0])
        self.assertEqual(first.kwargs["buttons"], put_forward_buttons("first"))
        self.assertFalse(first.kwargs["silent"])

    def test_command_says_so_when_nothing_waits(self):
        self.decide("plain", put_forward=False)

        self.command()

        self.assertEqual(self.sent()[0].args[0], NOTHING_PENDING)
        self.assertEqual(self.sent()[0].kwargs["reply_to"], 10)

    def test_rated_archived_and_old_mails_no_longer_wait(self):
        self.decide("old")
        self.now += timedelta(days=8)
        self.decide("rated")
        self.store.record_feedback("rated", "valid", origin="list")
        self.decide("archived")
        self.mail.archive_message("archived")
        self.decide("waiting")

        self.command()

        self.assertEqual(self.subjects(), ["subject waiting"])

    def test_a_mail_whose_state_cannot_be_read_stays_listed(self):
        self.decide("unknown")
        self.mail.in_inbox = MagicMock(side_effect=RuntimeError("gmail down"))

        with self.assertLogs("src.interactions.put_forward", level="WARNING"):
            self.command()

        self.assertEqual(self.subjects(), ["subject unknown"])

    def test_daily_list_is_silent_and_starts_with_a_header(self):
        self.decide("first")
        self.decide("second")

        sent = self.list.send_daily(interactive=True)

        self.assertEqual(sent, 2)
        self.assertEqual(self.sent()[0].args[0], "👀 À voir : 2 nouveaux mails")
        self.assertTrue(all(call.kwargs["silent"] for call in self.sent()))
        self.assertEqual(self.sent()[1].kwargs["buttons"], put_forward_buttons("first"))

    def test_daily_list_only_shows_what_is_new_since_the_previous_one(self):
        self.decide("yesterday")
        self.list.send_daily(interactive=True)
        self.chat.send_message.reset_mock()
        self.decide("today")

        sent = self.list.send_daily(interactive=True)

        self.assertEqual(sent, 1)
        self.assertEqual(
            self.sent()[0].args[0],
            "👀 À voir : 1 nouveau mail · 1 plus ancien(s) en attente · /avoir pour tout voir",
        )
        self.assertEqual(self.subjects(), ["subject today"])

    def test_nothing_new_sends_nothing(self):
        self.decide("plain", put_forward=False)

        self.assertEqual(self.list.send_daily(interactive=True), 0)
        self.chat.send_message.assert_not_called()

    def test_daily_list_is_capped_and_the_rest_comes_with_the_next_one(self):
        for index in range(DAILY_ITEMS + 2):
            self.decide(f"m{index}")

        sent = self.list.send_daily(interactive=True)

        self.assertEqual(sent, DAILY_ITEMS)
        self.assertEqual(len(self.sent()), DAILY_ITEMS + 1)
        self.assertIn("7 nouveaux mails · 2 reporté(s) à demain", self.sent()[0].args[0])
        self.chat.send_message.reset_mock()

        self.assertEqual(self.list.send_daily(interactive=True), 2)
        self.assertEqual(self.subjects(), ["subject m5", "subject m6"])
        self.assertIn("2 nouveaux mails · 5 plus ancien(s) en attente", self.sent()[0].args[0])

    def test_a_failed_header_shows_the_same_mails_next_time(self):
        self.decide("first")
        self.chat.send_message.side_effect = RuntimeError("telegram down")

        with self.assertRaises(RuntimeError):
            self.list.send_daily(interactive=True)

        self.assertIsNone(self.store.get_state(LAST_SENT_KEY))
        self.chat.send_message.side_effect = None
        self.assertEqual(self.list.send_daily(interactive=True), 1)

    def test_a_failure_halfway_resumes_at_the_first_mail_that_did_not_go_out(self):
        for name in ("first", "second", "third"):
            self.decide(name)
        self.chat.send_message.side_effect = [1, 2, RuntimeError("timeout")]

        with self.assertRaises(RuntimeError):
            self.list.send_daily(interactive=True)
        self.chat.send_message.reset_mock(side_effect=True)

        self.assertEqual(self.list.send_daily(interactive=True), 2)
        self.assertEqual(self.subjects(), ["subject second", "subject third"])

    def test_command_shows_the_latest_ten_and_says_how_many_it_left_out(self):
        for index in range(12):
            self.decide(f"m{index:02d}")

        self.command()

        self.assertEqual(self.subjects()[0], "subject m02")
        self.assertEqual(len(self.sent()), 11)
        self.assertEqual(self.sent()[-1].args[0], "… et 2 plus ancien(s) non affiché(s).")

    def test_without_the_listener_the_daily_list_has_no_buttons(self):
        self.decide("first")

        self.list.send_daily(interactive=False)

        self.assertIsNone(self.sent()[1].kwargs["buttons"])

    def test_buttons_store_the_verdict_with_its_origin_and_clear_themselves(self):
        self.decide("useful")
        self.decide("noise")

        self.handler.dispatch(CallbackEvent("cb", 77, "pf:v:useful"))
        self.handler.dispatch(CallbackEvent("cb", 78, "pf:n:noise"))

        rows = self.store._conn.execute(
            "SELECT message_id, verdict, origin FROM feedback ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("useful", "valid", "list"), ("noise", "false_important", "list")],
        )
        self.chat.answer_callback.assert_called_with("cb", "Noté : pas utile")
        self.assertEqual(self.sent(), [])
        self.chat.clear_buttons.assert_called_with(78)
        self.chat.send_message.reset_mock()
        self.command()
        self.assertEqual(self.sent()[0].args[0], NOTHING_PENDING)

    def test_buttons_only_rate_a_mail_that_was_put_forward(self):
        self.decide("plain", put_forward=False)

        self.handler.dispatch(CallbackEvent("cb", 77, "pf:n:plain"))

        self.chat.answer_callback.assert_called_with("cb", "Mail inconnu")
        self.assertEqual(self.store.feedback_counts()["false_important"], 0)

    def test_command_exists_only_when_mails_are_put_forward(self):
        without = InteractionHandler(self.store, self.chat, self.mail)

        self.assertIn("/avoir", self.handler.commands.help_text())
        self.assertNotIn("/avoir", without.commands.help_text())
        self.assertIsNone(without.put_forward)


if __name__ == "__main__":
    unittest.main()
