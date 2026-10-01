import logging
from collections.abc import Callable
from dataclasses import dataclass

from src.domain import CommandEvent
from src.observability.metrics import Metrics
from src.ports import ChatInbox

logger = logging.getLogger(__name__)

UNKNOWN_COMMAND = "Commande inconnue."
COMMAND_FAILED = "La commande a échoué, réessaie plus tard."
HELP_HEADER = "Commandes disponibles :"
# Telegram sends /start when a chat with the bot is opened.
HELP_ALIASES = ("help", "start")
UNKNOWN_LABEL = "unknown"


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    run: Callable[[CommandEvent], None]


class CommandRouter:
    def __init__(self, chat: ChatInbox) -> None:
        self._chat = chat
        self._commands: dict[str, Command] = {}

    def register(self, name: str, description: str, run: Callable[[CommandEvent], None]) -> None:
        if name in self._commands or name in HELP_ALIASES:
            raise ValueError(f"command /{name} is already registered")
        self._commands[name] = Command(name, description, run)

    def label(self, name: str) -> str:
        """Metric label: command names are typed by the user, so unknown ones share one value."""
        return name if name in self._commands or name in HELP_ALIASES else UNKNOWN_LABEL

    def help_text(self) -> str:
        lines = [f"/{command.name} — {command.description}" for command in self._commands.values()]
        return "\n".join([HELP_HEADER, *lines, "/help — cette liste"])

    def dispatch(self, event: CommandEvent) -> None:
        if event.name in HELP_ALIASES:
            self._reply(event, self.help_text())
            Metrics.mark_chat_command(event.name, "handled")
            return
        command = self._commands.get(event.name)
        if command is None:
            self._reply(event, f"{UNKNOWN_COMMAND}\n\n{self.help_text()}")
            Metrics.mark_chat_command(UNKNOWN_LABEL, "unknown")
            return
        try:
            command.run(event)
        except Exception:
            logger.exception("Chat command /%s failed", command.name)
            Metrics.mark_chat_command(command.name, "failed")
            self._reply(event, COMMAND_FAILED)
            return
        Metrics.mark_chat_command(command.name, "handled")

    def _reply(self, event: CommandEvent, text: str) -> None:
        try:
            self._chat.send_message(text, reply_to=event.message_id)
        except Exception as exc:  # noqa: BLE001 - the answer is informative, nothing depends on it
            logger.warning("Failed to answer chat command /%s: %s", event.name, exc)
