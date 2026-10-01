import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from prometheus_client import REGISTRY

from src.domain import CommandEvent
from src.interactions import InteractionHandler
from src.interactions.commands import COMMAND_FAILED, UNKNOWN_COMMAND, CommandRouter
from src.storage import SqliteDecisionStore


def command(name: str, args: str = "", message_id: int = 10) -> CommandEvent:
    return CommandEvent(message_id=message_id, name=name, args=args)


def counted(name: str, status: str) -> float:
    labels = {"command": name, "status": status}
    return REGISTRY.get_sample_value("chat_commands_total", labels) or 0.0


class CommandRouterTests(unittest.TestCase):
    def setUp(self):
        self.chat = MagicMock()
        self.router = CommandRouter(self.chat)
        self.ran: list[CommandEvent] = []
        self.router.register("review", "mails à vérifier", self.ran.append)

    def sent(self) -> str:
        return self.chat.send_message.call_args.args[0]

    def test_runs_the_registered_command_with_its_event(self):
        event = command("review", "3")
        before = counted("review", "handled")

        self.router.dispatch(event)

        self.assertEqual(self.ran, [event])
        self.assertEqual(counted("review", "handled"), before + 1)

    def test_help_and_start_list_every_command(self):
        for name in ("help", "start"):
            with self.subTest(name=name):
                self.router.dispatch(command(name))

                self.assertIn("/review — mails à vérifier", self.sent())
                self.assertIn("/help", self.sent())
                self.assertEqual(self.chat.send_message.call_args.kwargs["reply_to"], 10)

    def test_unknown_command_gets_the_help_and_one_shared_metric_label(self):
        before = counted("unknown", "unknown")

        self.router.dispatch(command("rm_rf"))

        self.assertTrue(self.sent().startswith(UNKNOWN_COMMAND))
        self.assertIn("/review", self.sent())
        self.assertEqual(counted("unknown", "unknown"), before + 1)
        self.assertEqual(self.router.label("rm_rf"), "unknown")

    def test_failing_command_is_reported_and_never_raises(self):
        self.router.register("boom", "explose", MagicMock(side_effect=RuntimeError("x")))
        before = counted("boom", "failed")

        with self.assertLogs("src.interactions.commands", level="ERROR"):
            self.router.dispatch(command("boom"))

        self.assertEqual(self.sent(), COMMAND_FAILED)
        self.assertEqual(counted("boom", "failed"), before + 1)

    def test_chat_failure_while_answering_is_only_logged(self):
        self.chat.send_message.side_effect = RuntimeError("telegram down")

        with self.assertLogs("src.interactions.commands", level="WARNING"):
            self.router.dispatch(command("help"))

    def test_a_name_cannot_be_registered_twice_or_shadow_help(self):
        for name in ("review", "help", "start"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.router.register(name, "again", self.ran.append)


class CommandDispatchTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteDecisionStore(":memory:")
        self.addCleanup(self.store.close)
        self.chat = MagicMock()
        self.handler = InteractionHandler(self.store, self.chat, MagicMock())
        self.ran: list[CommandEvent] = []
        self.handler.commands.register("ping", "test", self.ran.append)

    def test_command_event_reaches_the_router(self):
        self.handler.dispatch(command("ping"))

        self.assertEqual(len(self.ran), 1)

    def test_redelivered_command_runs_once(self):
        before = counted("ping", "duplicate")

        self.handler.dispatch(command("ping", message_id=11))
        self.handler.dispatch(command("ping", message_id=11))
        self.handler.dispatch(command("ping", message_id=12))

        self.assertEqual(len(self.ran), 2)
        self.assertEqual(counted("ping", "duplicate"), before + 1)

    def test_command_dedup_state_is_pruned_with_the_other_dedup_keys(self):
        self.handler.dispatch(command("ping", message_id=11))

        self.assertEqual(self.store.prune(timedelta(seconds=-1)), 1)
        self.assertIsNone(self.store.get_state("cmd:11"))


if __name__ == "__main__":
    unittest.main()
