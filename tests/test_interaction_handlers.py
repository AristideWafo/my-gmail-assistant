import unittest
from unittest.mock import MagicMock, patch

from prometheus_client import REGISTRY

from src.gateways.telegram_bot import CallbackEvent, ReplyEvent, TelegramApiError
from src.gmail.client import EmailMessage
from src.interactions import InteractionHandler
from src.interactions.callbacks import (
    Callback,
    draft_buttons,
    feedback_buttons,
    parse_callback,
)
from src.interactions.handlers import REPLY_HINT, SEND_FAILED
from src.observability.metrics import Metrics
from src.storage import DecisionStore
from src.triage.engine import TriageResult

ALERT_MESSAGE_ID = 500
PREVIEW_MESSAGE_ID = 901


def make_email() -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="t1",
        sender="celine@outlook.com",
        subject="Rencontre demain",
        snippet="s",
        body="b",
        message_id_header="<abc@mail.example>",
    )


def callback(data: str, message_id: int = ALERT_MESSAGE_ID) -> CallbackEvent:
    return CallbackEvent(callback_id="cb", message_id=message_id, data=data)


def reply(text: str = "Oui, 14h me va.", message_id: int = 900, to: int = ALERT_MESSAGE_ID):
    return ReplyEvent(message_id=message_id, reply_to_message_id=to, text=text)


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.record_decision(make_email(), TriageResult("high", "personnel", 0.9), "llm")
        self.store.attach_chat_message("m1", ALERT_MESSAGE_ID)
        self.bot = MagicMock()
        self.bot.send_message.return_value = PREVIEW_MESSAGE_ID
        self.gmail = MagicMock()
        self.gmail.create_draft.return_value = {"id": "r-42"}
        self.gmail.send_draft.return_value = {"id": "sent"}
        self.handler = InteractionHandler(self.store, self.bot, self.gmail)
        metrics = patch("src.interactions.handlers.Metrics")
        self.metrics = metrics.start()
        self.addCleanup(metrics.stop)

    def answered(self) -> str:
        return self.bot.answer_callback.call_args.args[1]

    def reply_statuses(self) -> list[str]:
        return [c.args[0] for c in self.metrics.mark_chat_reply.call_args_list]

    def offer_draft(self, draft_id: str = "r-42", preview_id: int = PREVIEW_MESSAGE_ID) -> None:
        self.store.set_state(f"bot_draft:{draft_id}", str(preview_id))


class FeedbackTests(HandlerTestCase):
    def test_each_verdict_is_stored_counted_acknowledged_and_buttons_cleared(self):
        for code, verdict in (("v", "valid"), ("u", "false_urgent"), ("s", "false_spam")):
            with self.subTest(verdict=verdict):
                self.handler.dispatch(callback(f"fb:{code}:m1"))

                self.assertEqual(self.store.feedback_counts()[verdict], 1)
                self.metrics.mark_feedback.assert_called_with(verdict)
                self.assertTrue(self.answered())
                self.bot.clear_buttons.assert_called_with(ALERT_MESSAGE_ID)

    def test_unknown_mail_is_reported_and_not_counted(self):
        self.handler.dispatch(callback("fb:v:missing"))

        self.assertEqual(self.answered(), "Mail inconnu")
        self.metrics.mark_feedback.assert_not_called()
        self.bot.clear_buttons.assert_not_called()

    def test_telegram_failure_on_ack_still_clears_buttons(self):
        self.bot.answer_callback.side_effect = TelegramApiError("answerCallbackQuery failed")

        with self.assertLogs("src.interactions.handlers", level="WARNING"):
            self.handler.dispatch(callback("fb:u:m1"))

        self.assertEqual(self.store.feedback_counts()["false_urgent"], 1)
        self.bot.clear_buttons.assert_called_once_with(ALERT_MESSAGE_ID)


class UnknownCallbackTests(HandlerTestCase):
    def test_malformed_data_gets_a_generic_answer_and_a_warning(self):
        for data in ("nope", "fb:x:m1", "fb:v:", "send:", "archive:m1"):
            with self.subTest(data=data), self.assertLogs(
                "src.interactions.handlers", level="WARNING"
            ):
                self.handler.dispatch(callback(data))

                self.assertEqual(self.answered(), "Action non reconnue")
        self.gmail.send_draft.assert_not_called()


class ReplyTests(HandlerTestCase):
    def test_reply_creates_a_threaded_draft_and_offers_send_or_cancel(self):
        self.handler.dispatch(reply())

        self.gmail.create_draft.assert_called_once_with(
            "t1",
            "celine@outlook.com",
            "Rencontre demain",
            "Oui, 14h me va.",
            in_reply_to="<abc@mail.example>",
        )
        text = self.bot.send_message.call_args.args[0]
        self.assertIn("celine@outlook.com", text)
        self.assertIn("Rencontre demain", text)
        self.assertIn("Oui, 14h me va.", text)
        self.assertEqual(
            self.bot.send_message.call_args.kwargs["buttons"],
            [[("Envoyer", "send:r-42"), ("Annuler", "cancel:r-42")]],
        )
        self.assertEqual(self.bot.send_message.call_args.kwargs["reply_to"], 900)
        self.assertEqual(self.reply_statuses(), ["drafted"])
        self.assertEqual(self.store.get_state("bot_draft:r-42"), str(PREVIEW_MESSAGE_ID))

    def test_long_reply_is_truncated_in_the_preview_only(self):
        self.handler.dispatch(reply(text="x" * 2000))

        self.assertEqual(len(self.gmail.create_draft.call_args.args[3]), 2000)
        self.assertLess(len(self.bot.send_message.call_args.args[0]), 700)

    def test_redelivered_reply_creates_a_single_draft(self):
        self.handler.dispatch(reply())
        self.handler.dispatch(reply())

        self.gmail.create_draft.assert_called_once()
        self.assertEqual(self.bot.send_message.call_count, 1)
        self.assertEqual(self.reply_statuses(), ["drafted", "duplicate"])

    def test_reply_to_a_non_alert_message_explains_how_to_reply(self):
        self.handler.dispatch(reply(to=12345))

        self.gmail.create_draft.assert_not_called()
        self.bot.send_message.assert_called_once_with(REPLY_HINT, buttons=None, reply_to=900)
        self.assertEqual(self.reply_statuses(), ["unknown_target"])

    def test_draft_failure_is_reported_and_retried_on_next_reply(self):
        self.gmail.create_draft.side_effect = RuntimeError("gmail down")

        with self.assertLogs("src.interactions.handlers", level="ERROR"):
            self.handler.dispatch(reply())

        self.assertIn("Impossible", self.bot.send_message.call_args.args[0])
        self.assertIsNone(self.store.get_state("reply:900"))
        self.assertEqual(self.reply_statuses(), ["draft_failed"])

    def test_unconfigured_gmail_counts_as_draft_failure(self):
        self.gmail.create_draft.return_value = None

        self.handler.dispatch(reply())

        self.assertEqual(self.reply_statuses(), ["draft_failed"])

    def test_preview_failure_keeps_the_draft_and_blocks_duplicates(self):
        self.bot.send_message.side_effect = TelegramApiError("Telegram sendMessage failed")

        with self.assertLogs("src.interactions.handlers", level="WARNING"):
            self.handler.dispatch(reply())
        self.handler.dispatch(reply())

        self.gmail.create_draft.assert_called_once()
        self.assertIsNone(self.store.get_state("bot_draft:r-42"))

    def test_oversized_draft_id_is_previewed_without_buttons(self):
        self.gmail.create_draft.return_value = {"id": "r" * 80}

        self.handler.dispatch(reply())

        self.assertIsNone(self.bot.send_message.call_args.kwargs["buttons"])
        self.assertIn("depuis Gmail", self.bot.send_message.call_args.args[0])
        self.assertIsNone(self.store.get_state(f"bot_draft:{'r' * 80}"))


class SendTests(HandlerTestCase):
    def setUp(self):
        super().setUp()
        self.offer_draft()

    def test_send_delivers_the_draft_once_and_clears_buttons(self):
        self.handler.dispatch(callback("send:r-42", message_id=901))

        self.gmail.send_draft.assert_called_once_with("r-42")
        self.assertEqual(self.answered(), "Envoyé")
        self.bot.clear_buttons.assert_called_once_with(901)
        self.assertEqual(self.reply_statuses(), ["sent"])

    def test_second_press_never_sends_twice(self):
        self.handler.dispatch(callback("send:r-42", message_id=901))
        self.handler.dispatch(callback("send:r-42", message_id=901))

        self.gmail.send_draft.assert_called_once()
        self.assertEqual(self.answered(), "Déjà envoyé")
        self.assertEqual(self.bot.clear_buttons.call_count, 2)
        self.assertEqual(self.reply_statuses(), ["sent", "duplicate"])

    def test_send_is_claimed_before_gmail_is_called(self):
        def send(draft_id):
            self.assertIsNotNone(self.store.get_state(f"draft_sent:{draft_id}"))
            return {"id": "sent"}

        self.gmail.send_draft.side_effect = send

        self.handler.dispatch(callback("send:r-42", message_id=901))

        self.gmail.send_draft.assert_called_once()

    def test_failed_send_is_reported_and_never_retried_automatically(self):
        self.gmail.send_draft.side_effect = TimeoutError("gmail timed out")

        with self.assertLogs("src.interactions.handlers", level="ERROR"):
            self.handler.dispatch(callback("send:r-42", message_id=901))
        self.handler.dispatch(callback("send:r-42", message_id=901))

        self.gmail.send_draft.assert_called_once()
        self.assertEqual(self.bot.answer_callback.call_args_list[0].args[1], SEND_FAILED)
        self.assertEqual(self.answered(), "Déjà envoyé")
        self.bot.clear_buttons.assert_called_with(901)
        self.assertEqual(self.reply_statuses(), ["send_failed", "duplicate"])

    def test_unconfigured_gmail_is_a_failed_send(self):
        self.gmail.send_draft.return_value = None

        with self.assertLogs("src.interactions.handlers", level="ERROR"):
            self.handler.dispatch(callback("send:r-42", message_id=901))

        self.assertEqual(self.reply_statuses(), ["send_failed"])

    def test_send_failure_text_does_not_claim_the_mail_was_not_sent(self):
        self.assertIn("Envoyés", SEND_FAILED)
        self.assertNotIn("Échec", SEND_FAILED)


class DraftOwnershipTests(HandlerTestCase):
    def test_forged_draft_id_is_never_sent_or_cancelled(self):
        for data in ("send:r-forged", "cancel:r-forged"):
            with self.subTest(data=data), self.assertLogs(
                "src.interactions.handlers", level="WARNING"
            ):
                self.handler.dispatch(callback(data, message_id=PREVIEW_MESSAGE_ID))

                self.assertEqual(self.answered(), "Action non reconnue")
        self.gmail.send_draft.assert_not_called()
        self.bot.clear_buttons.assert_not_called()
        self.assertIsNone(self.store.get_state("draft_sent:r-forged"))
        self.assertEqual(self.reply_statuses(), ["rejected", "rejected"])

    def test_offered_draft_pressed_from_another_message_is_rejected(self):
        self.offer_draft()

        with self.assertLogs("src.interactions.handlers", level="WARNING"):
            self.handler.dispatch(callback("send:r-42", message_id=ALERT_MESSAGE_ID))

        self.gmail.send_draft.assert_not_called()
        self.assertIsNone(self.store.get_state("draft_sent:r-42"))
        self.assertEqual(self.reply_statuses(), ["rejected"])

    def test_reply_then_send_from_its_preview_goes_through(self):
        self.handler.dispatch(reply())
        self.handler.dispatch(callback("send:r-42", message_id=PREVIEW_MESSAGE_ID))

        self.gmail.send_draft.assert_called_once_with("r-42")
        self.assertEqual(self.reply_statuses(), ["drafted", "sent"])


class CancelTests(HandlerTestCase):
    def test_cancel_keeps_the_draft_and_clears_buttons(self):
        self.offer_draft()

        self.handler.dispatch(callback("cancel:r-42", message_id=901))

        self.gmail.send_draft.assert_not_called()
        self.bot.clear_buttons.assert_called_once_with(901)
        self.assertEqual(self.answered(), "Brouillon conservé dans Gmail")
        self.assertEqual(self.reply_statuses(), ["cancelled"])


class InteractionMetricsTests(unittest.TestCase):
    def test_feedback_and_chat_reply_counters_are_labelled(self):
        def value(name, labels):
            return REGISTRY.get_sample_value(name, labels) or 0.0

        feedback_before = value("feedback_total", {"verdict": "false_spam"})
        replies_before = value("chat_replies_total", {"status": "sent"})

        Metrics.mark_feedback("false_spam")
        Metrics.mark_chat_reply("sent")

        self.assertEqual(value("feedback_total", {"verdict": "false_spam"}), feedback_before + 1)
        self.assertEqual(value("chat_replies_total", {"status": "sent"}), replies_before + 1)


class CallbackCodecTests(unittest.TestCase):
    def test_feedback_buttons_round_trip(self):
        row = feedback_buttons("18c2f0a1b2c3d4e5")[0]

        self.assertEqual(
            [parse_callback(data) for _, data in row],
            [
                Callback("fb", "18c2f0a1b2c3d4e5", "valid"),
                Callback("fb", "18c2f0a1b2c3d4e5", "false_urgent"),
                Callback("fb", "18c2f0a1b2c3d4e5", "false_spam"),
            ],
        )

    def test_draft_buttons_round_trip(self):
        row = draft_buttons("r-42")[0]

        self.assertEqual(
            [parse_callback(data) for _, data in row],
            [Callback("send", "r-42"), Callback("cancel", "r-42")],
        )

    def test_buttons_are_dropped_when_callback_data_exceeds_64_bytes(self):
        self.assertIsNone(feedback_buttons("x" * 60))
        self.assertIsNone(draft_buttons("x" * 60))
        self.assertIsNotNone(feedback_buttons("x" * 59))

    def test_unknown_or_incomplete_data_is_rejected(self):
        for data in ("", "fb", "fb:v", "fb:z:m1", "send", "cancel:", "other:1"):
            with self.subTest(data=data):
                self.assertIsNone(parse_callback(data))


if __name__ == "__main__":
    unittest.main()
