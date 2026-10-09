import unittest
from unittest.mock import MagicMock

import requests
from prometheus_client import REGISTRY

from src.domain import CallbackEvent, CommandEvent, ReplyEvent, TextEvent
from src.gateways.telegram_bot import TelegramApiError, TelegramBot, TelegramChannel
from src.ports import AlertChannel, ChannelDeliveryError, ChatInbox

TOKEN = "123456:SECRET-token"
CHAT_ID = "42"


def api_response(result, ok=True, status=200) -> MagicMock:
    response = MagicMock(status_code=status)
    response.json.return_value = {"ok": ok, "result": result}
    return response


def make_bot(*responses, chat_id=CHAT_ID, **kwargs) -> tuple[TelegramBot, MagicMock]:
    http = MagicMock()
    http.post.side_effect = list(responses)
    return TelegramBot(TOKEN, chat_id, http=http, poll_timeout=25, **kwargs), http


def callback_update(update_id, chat_id=42, data="archive:1", message_id=7, user_id=42) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def reply_update(update_id, chat_id=42, text="plus tard", reply_to=7, user_id=42) -> dict:
    message = {"message_id": 99, "chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def rejected(reason: str) -> float:
    return REGISTRY.get_sample_value("telegram_inbound_rejected_total", {"reason": reason}) or 0.0


def poll_errors() -> float:
    return REGISTRY.get_sample_value("telegram_poll_errors_total") or 0.0


def last_poll() -> float:
    return REGISTRY.get_sample_value("telegram_last_poll_timestamp_seconds") or 0.0


class ConfiguredTests(unittest.TestCase):
    def test_requires_token_and_chat_id(self):
        self.assertTrue(TelegramBot(TOKEN, CHAT_ID).is_configured)
        self.assertFalse(TelegramBot("", CHAT_ID).is_configured)
        self.assertFalse(TelegramBot(TOKEN, "").is_configured)

    def test_bot_is_a_chat_inbox(self):
        self.assertIsInstance(TelegramBot(TOKEN, CHAT_ID), ChatInbox)


class SendMessageTests(unittest.TestCase):
    def test_payload_has_plain_text_inline_keyboard_and_returns_message_id(self):
        bot, http = make_bot(api_response({"message_id": 321}))

        message_id = bot.send_message("Urgent", buttons=[[("Archiver", "a:1"), ("Lu", "r:1")]])

        self.assertEqual(message_id, 321)
        url = http.post.call_args.args[0]
        payload = http.post.call_args.kwargs["json"]
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertEqual(
            payload,
            {
                "chat_id": CHAT_ID,
                "text": "Urgent",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "Archiver", "callback_data": "a:1"},
                            {"text": "Lu", "callback_data": "r:1"},
                        ]
                    ]
                },
            },
        )

    def test_silent_message_asks_telegram_not_to_notify(self):
        bot, http = make_bot(api_response({"message_id": 5}), api_response({"message_id": 6}))

        bot.send_message("list", silent=True)
        silent = http.post.call_args.kwargs["json"]
        bot.send_message("alert")
        loud = http.post.call_args.kwargs["json"]

        self.assertIs(silent["disable_notification"], True)
        self.assertNotIn("disable_notification", loud)

    def test_without_buttons_has_no_markup_and_reply_to_is_forwarded(self):
        bot, http = make_bot(api_response({"message_id": 5}))

        bot.send_message("ok", reply_to=4)

        payload = http.post.call_args.kwargs["json"]
        self.assertNotIn("reply_markup", payload)
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(payload["reply_parameters"], {"message_id": 4})

    def test_http_failure_raises_request_exception_without_token(self):
        response = MagicMock(status_code=401)
        response.raise_for_status.side_effect = requests.HTTPError(
            f"401 for url: https://api.telegram.org/bot{TOKEN}/sendMessage", response=response
        )
        bot, _ = make_bot(response)

        with self.assertRaises(requests.RequestException) as ctx:
            bot.send_message("x")

        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertIn("401", str(ctx.exception))

    def test_api_level_rejection_raises(self):
        bot, _ = make_bot(api_response(None, ok=False))

        with self.assertRaises(TelegramApiError):
            bot.send_message("x")

    def test_unexpected_result_shape_raises(self):
        for result in (None, True, [], {}, {"message_id": "5"}, {"message_id": True}):
            with self.subTest(result=result):
                bot, _ = make_bot(api_response(result))

                with self.assertRaises(TelegramApiError):
                    bot.send_message("x")


class CallbackActionsTests(unittest.TestCase):
    def test_answer_callback(self):
        bot, http = make_bot(api_response(True))

        bot.answer_callback("cb-1", "Archivé")

        self.assertTrue(http.post.call_args.args[0].endswith("/answerCallbackQuery"))
        self.assertEqual(
            http.post.call_args.kwargs["json"], {"callback_query_id": "cb-1", "text": "Archivé"}
        )

    def test_clear_buttons_sends_empty_keyboard(self):
        bot, http = make_bot(api_response(True))

        bot.clear_buttons(7)

        self.assertTrue(http.post.call_args.args[0].endswith("/editMessageReplyMarkup"))
        self.assertEqual(
            http.post.call_args.kwargs["json"],
            {"chat_id": CHAT_ID, "message_id": 7, "reply_markup": {"inline_keyboard": []}},
        )

    def test_edit_message_rewrites_the_text_and_resends_the_keyboard(self):
        bot, http = make_bot(api_response({"message_id": 7}), api_response(True))

        bot.edit_message(7, "updated", buttons=[[("OK", "fb:v:1")]])
        bot.edit_message(7, "plain")

        first, second = (call.kwargs["json"] for call in http.post.call_args_list)
        self.assertTrue(http.post.call_args.args[0].endswith("/editMessageText"))
        self.assertEqual(
            first,
            {
                "chat_id": CHAT_ID,
                "message_id": 7,
                "text": "updated",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": {"inline_keyboard": [[{"text": "OK", "callback_data": "fb:v:1"}]]},
            },
        )
        self.assertNotIn("reply_markup", second)
        self.assertEqual(second["link_preview_options"], {"is_disabled": True})

    def test_get_me_returns_the_bot_profile(self):
        bot, http = make_bot(api_response({"username": "mybot"}))

        self.assertEqual(bot.get_me(), {"username": "mybot"})
        self.assertTrue(http.post.call_args.args[0].endswith("/getMe"))


class GetUpdatesTests(unittest.TestCase):
    def test_long_poll_parameters(self):
        bot, http = make_bot(api_response([]))

        bot.get_updates(10)

        self.assertEqual(
            http.post.call_args.kwargs["json"],
            {"timeout": 25, "allowed_updates": ["message", "callback_query"], "offset": 10},
        )
        self.assertEqual(http.post.call_args.kwargs["timeout"], 35)

    def test_no_offset_is_omitted_and_empty_batch_keeps_it(self):
        bot, http = make_bot(api_response([]))

        self.assertEqual(bot.get_updates(None), ([], None))
        self.assertNotIn("offset", http.post.call_args.kwargs["json"])

    def test_parses_callback_and_reply(self):
        bot, _ = make_bot(api_response([callback_update(3), reply_update(4)]))

        events, offset = bot.get_updates(3)

        self.assertEqual(
            events,
            [
                CallbackEvent(callback_id="cb-1", message_id=7, data="archive:1"),
                ReplyEvent(message_id=99, reply_to_message_id=7, text="plus tard"),
            ],
        )
        self.assertEqual(offset, 5)

    def test_foreign_chat_is_ignored_logged_at_debug_by_id_only_and_counted(self):
        updates = [callback_update(8, chat_id=666), reply_update(9, chat_id=666, text="secret")]
        bot, _ = make_bot(api_response(updates))
        before = rejected("foreign_chat")

        with self.assertLogs("src.gateways.telegram_bot", level="DEBUG") as logs:
            events, offset = bot.get_updates(None)

        self.assertEqual(events, [])
        self.assertEqual(offset, 10)
        self.assertEqual(rejected("foreign_chat"), before + 2)
        self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
        output = "\n".join(logs.output)
        self.assertIn("666", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("archive:1", output)

    def test_callback_without_message_is_treated_as_foreign(self):
        update = {"update_id": 1, "callback_query": {"id": "x", "from": {"id": 42}, "data": "a"}}
        bot, _ = make_bot(api_response([update]))
        before = rejected("foreign_chat")

        events, offset = bot.get_updates(None)

        self.assertEqual((events, offset), ([], 2))
        self.assertEqual(rejected("foreign_chat"), before + 1)

    def test_text_that_is_neither_a_reply_nor_a_command_is_free_text(self):
        bot, _ = make_bot(api_response([reply_update(1, text="où en est le devis ?", reply_to=None)]))

        self.assertEqual(
            bot.get_updates(None), ([TextEvent(message_id=99, text="où en est le devis ?")], 2)
        )

    def test_messages_without_text_are_ignored(self):
        updates = [reply_update(1, text=""), reply_update(2, text="", reply_to=None)]
        bot, _ = make_bot(api_response(updates))

        self.assertEqual(bot.get_updates(None), ([], 3))

    def test_parses_commands_with_arguments_and_bot_suffix(self):
        updates = [
            reply_update(1, text="/review", reply_to=None),
            reply_update(2, text="/Review@my_bot  3 \n", reply_to=None),
            reply_update(3, text="/stats@my_bot", reply_to=None),
        ]
        bot, _ = make_bot(api_response(updates))

        events, _ = bot.get_updates(None)

        self.assertEqual(
            events,
            [
                CommandEvent(message_id=99, name="review", args=""),
                CommandEvent(message_id=99, name="review", args="3"),
                CommandEvent(message_id=99, name="stats", args=""),
            ],
        )

    def test_a_reply_starting_with_a_slash_stays_mail_text(self):
        bot, _ = make_bot(api_response([reply_update(1, text="/review plus tard")]))

        events, _ = bot.get_updates(None)

        self.assertEqual(
            events, [ReplyEvent(message_id=99, reply_to_message_id=7, text="/review plus tard")]
        )

    def test_text_that_only_looks_like_a_command_is_free_text(self):
        texts = ["/", "/!\\ attention", "voir /review"]
        updates = [reply_update(i, text=text, reply_to=None) for i, text in enumerate(texts, 1)]
        bot, _ = make_bot(api_response(updates))

        events, _ = bot.get_updates(None)

        self.assertEqual(events, [TextEvent(message_id=99, text=text) for text in texts])

    def test_free_text_from_an_unauthorized_user_is_rejected(self):
        bot, _ = make_bot(api_response([reply_update(1, text="bonjour", reply_to=None, user_id=7)]))

        self.assertEqual(bot.get_updates(None), ([], 2))

    def test_command_from_an_unauthorized_user_is_rejected(self):
        bot, _ = make_bot(api_response([reply_update(1, text="/review", reply_to=None, user_id=7)]))
        before = rejected("unauthorized_user")

        self.assertEqual(bot.get_updates(None), ([], 2))
        self.assertEqual(rejected("unauthorized_user"), before + 1)

    def test_oversized_or_non_string_callback_data_is_skipped(self):
        updates = [callback_update(1, data="é" * 33), callback_update(2, data=12)]
        bot, _ = make_bot(api_response(updates))
        before = rejected("malformed")

        events, offset = bot.get_updates(None)

        self.assertEqual((events, offset), ([], 3))
        self.assertEqual(rejected("malformed"), before + 2)

    def test_callback_data_at_the_64_byte_limit_is_accepted(self):
        bot, _ = make_bot(api_response([callback_update(1, data="x" * 64)]))

        events, _ = bot.get_updates(None)

        self.assertEqual(len(events), 1)

    def test_unsupported_and_malformed_updates_are_skipped_and_counted(self):
        updates = [{"update_id": 1, "edited_message": {"chat": {"id": 42}}}, {"no_id": True}]
        bot, _ = make_bot(api_response(updates))
        before = rejected("malformed")

        self.assertEqual(bot.get_updates(None), ([], 2))
        self.assertEqual(rejected("malformed"), before + 2)

    def test_non_list_result_raises(self):
        bot, _ = make_bot(api_response({"update_id": 1}))

        with self.assertRaises(TelegramApiError):
            bot.get_updates(None)


class InboundAuthorizationTests(unittest.TestCase):
    def test_private_chat_defaults_to_its_own_user(self):
        self.assertTrue(TelegramBot(TOKEN, "42").inbound_authorized)

    def test_group_chat_needs_an_explicit_allowlist(self):
        self.assertFalse(TelegramBot(TOKEN, "-100123").inbound_authorized)
        self.assertFalse(TelegramBot(TOKEN, "@channel").inbound_authorized)
        self.assertFalse(TelegramBot(TOKEN, "").inbound_authorized)
        self.assertTrue(
            TelegramBot(TOKEN, "-100123", allowed_user_ids=frozenset({7})).inbound_authorized
        )

    def test_other_user_in_the_right_chat_is_rejected(self):
        updates = [callback_update(1, user_id=666), reply_update(2, user_id=666)]
        bot, _ = make_bot(api_response(updates))
        before = rejected("unauthorized_user")

        self.assertEqual(bot.get_updates(None), ([], 3))
        self.assertEqual(rejected("unauthorized_user"), before + 2)

    def test_missing_sender_is_rejected(self):
        callback, reply = callback_update(1), reply_update(2)
        del callback["callback_query"]["from"]
        del reply["message"]["from"]
        bot, _ = make_bot(api_response([callback, reply]))

        self.assertEqual(bot.get_updates(None), ([], 3))

    def test_anonymous_admin_or_channel_post_is_rejected(self):
        update = reply_update(1)
        update["message"]["sender_chat"] = {"id": 42}
        bot, _ = make_bot(api_response([update]))
        before = rejected("unauthorized_user")

        self.assertEqual(bot.get_updates(None), ([], 2))
        self.assertEqual(rejected("unauthorized_user"), before + 1)

    def test_explicit_allowlist_replaces_the_private_chat_default(self):
        updates = [
            callback_update(1, chat_id=-100, user_id=7),
            reply_update(2, chat_id=-100, user_id=8),
            reply_update(3, chat_id=-100, user_id=100),
        ]
        bot, _ = make_bot(api_response(updates), chat_id="-100", allowed_user_ids=frozenset({7, 8}))

        events, _ = bot.get_updates(None)

        self.assertEqual([type(e) for e in events], [CallbackEvent, ReplyEvent])

    def test_group_without_allowlist_accepts_nobody(self):
        update = callback_update(1, chat_id=-100, user_id=7)
        bot, _ = make_bot(api_response([update]), chat_id="-100")

        self.assertEqual(bot.get_updates(None), ([], 2))


class PollingObservabilityTests(unittest.TestCase):
    def test_poll_outcomes_are_measured(self):
        bot, _ = make_bot(requests.ConnectionError("down"), api_response([]))
        errors_before = poll_errors()

        with (
            self.assertLogs("src.gateways.telegram_bot", level="DEBUG"),
            self.assertRaises(TelegramApiError),
        ):
            bot.get_updates(None)
        self.assertEqual(poll_errors(), errors_before + 1)
        bot.get_updates(None)

        self.assertEqual(poll_errors(), errors_before + 1)
        self.assertGreater(last_poll(), 0)

    def test_token_never_logged_on_poll_failure(self):
        response = MagicMock(status_code=502)
        response.raise_for_status.side_effect = requests.HTTPError(
            f"502 for url: https://api.telegram.org/bot{TOKEN}/getUpdates", response=response
        )
        bot, _ = make_bot(
            response, requests.ConnectionError(f"Max retries with url: /bot{TOKEN}/getUpdates")
        )

        with self.assertLogs("src.gateways.telegram_bot", level="DEBUG") as logs:
            for _ in range(2):
                with self.assertRaises(TelegramApiError):
                    bot.get_updates(None)

        output = "\n".join(logs.output)
        self.assertNotIn(TOKEN, output)
        self.assertIn("HTTPError", output)
        self.assertIn("502", output)
        self.assertIn("ConnectionError", output)


class TelegramChannelTests(unittest.TestCase):
    def test_is_an_interactive_alert_channel(self):
        channel = TelegramChannel(TelegramBot(TOKEN, CHAT_ID))

        self.assertIsInstance(channel, AlertChannel)
        self.assertEqual(channel.name, "telegram")
        self.assertTrue(channel.interactive)
        self.assertTrue(channel.is_configured)
        self.assertFalse(TelegramChannel(TelegramBot("", CHAT_ID)).is_configured)

    def test_send_forwards_buttons_and_returns_the_message_id(self):
        bot, http = make_bot(api_response({"message_id": 321}))

        message_id = TelegramChannel(bot).send("Urgent", [[("Ok", "fb:v:1")]])

        self.assertEqual(message_id, 321)
        keyboard = http.post.call_args.kwargs["json"]["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard, [[{"text": "Ok", "callback_data": "fb:v:1"}]])

    def test_api_failure_becomes_a_sanitized_delivery_error(self):
        response = MagicMock(status_code=403)
        response.raise_for_status.side_effect = requests.HTTPError(
            f"403 for url: https://api.telegram.org/bot{TOKEN}/sendMessage", response=response
        )
        bot, _ = make_bot(response)

        with self.assertRaises(ChannelDeliveryError) as ctx:
            TelegramChannel(bot).send("x")

        self.assertEqual(
            str(ctx.exception), "Telegram sendMessage failed: HTTPError (status 403)"
        )
        self.assertIsNone(ctx.exception.__cause__)

    def test_update_edits_the_message_and_sanitizes_failures(self):
        bot = MagicMock()
        channel = TelegramChannel(bot)

        channel.update(7, "updated", [[("OK", "fb:v:1")]])
        bot.edit_message.assert_called_once_with(7, "updated", buttons=[[("OK", "fb:v:1")]])

        bot.edit_message.side_effect = TelegramApiError("Telegram editMessageText failed")
        with self.assertRaises(ChannelDeliveryError):
            channel.update(7, "updated")

    def test_check_connection_reports_the_bot_username_via_get_me(self):
        bot, http = make_bot(api_response({"username": "mybot"}))

        self.assertEqual(TelegramChannel(bot).check_connection(), "bot @mybot reachable")
        self.assertEqual(http.post.call_args.args[0], f"https://api.telegram.org/bot{TOKEN}/getMe")


if __name__ == "__main__":
    unittest.main()
