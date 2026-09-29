import threading
import unittest
from unittest.mock import MagicMock

import requests

from src.domain import CallbackEvent, ReplyEvent
from src.gateways.telegram_bot import TelegramBot
from src.interactions.listener import run_listener

TOKEN = "123456:SECRET-token"


class FakeStopping:
    """Stops the listener after a fixed number of loop iterations."""

    def __init__(self, iterations: int) -> None:
        self._remaining = iterations

    def is_set(self) -> bool:
        self._remaining -= 1
        return self._remaining < 0


class RunListenerTests(unittest.TestCase):
    def run_listener(self, inbox, iterations, dispatch=None, load_offset=lambda: None):
        saved, sleeps = [], []
        run_listener(
            inbox,
            dispatch or MagicMock(),
            load_offset,
            saved.append,
            FakeStopping(iterations),
            sleep=sleeps.append,
        )
        return saved, sleeps

    def test_returns_immediately_when_stopping_is_set(self):
        inbox = MagicMock()
        stopping = threading.Event()
        stopping.set()

        run_listener(inbox, MagicMock(), lambda: None, MagicMock(), stopping, sleep=MagicMock())

        inbox.get_updates.assert_not_called()

    def test_backoff_doubles_caps_at_60_and_resets_on_success(self):
        inbox = MagicMock()
        failures = [requests.ConnectionError()] * 8
        inbox.get_updates.side_effect = [*failures, ([], None), requests.ConnectionError()]

        with self.assertLogs("src.interactions.listener", level="WARNING"):
            _, sleeps = self.run_listener(inbox, 10)

        self.assertEqual(sleeps, [1, 2, 4, 8, 16, 32, 60, 60, 1])

    def test_offset_saved_after_dispatch_and_passed_to_next_poll(self):
        order = []
        inbox = MagicMock()
        event = ReplyEvent(1, 2, "t")
        inbox.get_updates.side_effect = [([event], 11), ([], 11)]
        saved = []

        def save(offset):
            order.append("save")
            saved.append(offset)

        run_listener(
            inbox,
            lambda e: order.append("dispatch"),
            lambda: 10,
            save,
            FakeStopping(2),
            sleep=MagicMock(),
        )

        self.assertEqual(order, ["dispatch", "save"])
        self.assertEqual(saved, [11])
        self.assertEqual([c.args[0] for c in inbox.get_updates.call_args_list], [10, 11])

    def test_offset_saved_when_batch_only_had_ignored_updates(self):
        inbox = MagicMock()
        inbox.get_updates.return_value = ([], 20)

        saved, _ = self.run_listener(inbox, 1, load_offset=lambda: 19)

        self.assertEqual(saved, [20])

    def test_dispatch_failure_does_not_stop_other_events_or_loop(self):
        inbox = MagicMock()
        first, second = ReplyEvent(1, 2, "a"), ReplyEvent(3, 4, "b")
        inbox.get_updates.side_effect = [([first, second], 5), ([], 5)]
        dispatch = MagicMock(side_effect=[RuntimeError("boom"), None])

        with self.assertLogs("src.interactions.listener", level="ERROR"):
            saved, _ = self.run_listener(inbox, 2, dispatch=dispatch)

        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(saved, [5])
        self.assertEqual(inbox.get_updates.call_count, 2)

    def test_poll_failure_logs_only_the_error_type(self):
        inbox = MagicMock()
        inbox.get_updates.side_effect = requests.ConnectionError(f"url: /bot{TOKEN}/getUpdates")

        with self.assertLogs("src.interactions.listener", level="WARNING") as logs:
            self.run_listener(inbox, 1)

        output = "\n".join(logs.output)
        self.assertNotIn(TOKEN, output)
        self.assertIn("ConnectionError", output)

    def test_token_never_logged_when_handler_propagates_bot_error(self):
        http = MagicMock()
        http.post.side_effect = requests.ConnectionError(f"url: /bot{TOKEN}/answerCallbackQuery")
        bot = TelegramBot(TOKEN, "42", http=http)
        inbox = MagicMock()
        inbox.get_updates.return_value = ([CallbackEvent("cb", 1, "d")], 2)

        with self.assertLogs("src.interactions.listener", level="ERROR") as logs:
            self.run_listener(inbox, 1, dispatch=lambda e: bot.answer_callback("cb"))

        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_unexpected_errors_never_escape(self):
        inbox = MagicMock()
        inbox.get_updates.side_effect = ValueError("bad json")
        failing_load = MagicMock(side_effect=OSError("disk"))

        with self.assertLogs("src.interactions.listener", level="WARNING"):
            _, sleeps = self.run_listener(inbox, 1, load_offset=failing_load)

        self.assertEqual(sleeps, [1])
        inbox.get_updates.assert_called_once_with(None)

    def test_save_failure_is_logged_and_loop_continues(self):
        inbox = MagicMock()
        inbox.get_updates.side_effect = [([], 3), ([], 3)]
        failing_save = MagicMock(side_effect=OSError())

        with self.assertLogs("src.interactions.listener", level="ERROR"):
            run_listener(
                inbox, MagicMock(), lambda: None, failing_save, FakeStopping(2), MagicMock()
            )

        self.assertEqual(inbox.get_updates.call_count, 2)


if __name__ == "__main__":
    unittest.main()
