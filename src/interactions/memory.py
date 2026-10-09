from itertools import groupby

from src.domain import CommandEvent
from src.ports import ChatInbox, MemoryNotes

NOTHING = "Aucune note en mémoire. Dis-moi « retiens que … » à propos d'un correspondant."
USAGE = "Pour oublier une note : /memory oublie <numéro>"
FORGOTTEN = "Note oubliée."
UNKNOWN_NOTE = "Aucune note ne porte ce numéro."
FORGET_WORDS = ("oublie", "forget")


class MemoryCommand:
    """Shows what was remembered and lets the user remove a note: the memory is theirs."""

    def __init__(self, notes: MemoryNotes, chat: ChatInbox) -> None:
        self._notes = notes
        self._chat = chat

    def run(self, event: CommandEvent) -> None:
        self._chat.send_message(self._answer(event.args.split()), reply_to=event.message_id)

    def _answer(self, words: list[str]) -> str:
        if not words:
            return self._listing()
        if len(words) != 2 or words[0].lower() not in FORGET_WORDS or not words[1].isdigit():
            return USAGE
        return FORGOTTEN if self._notes.forget(int(words[1])) else UNKNOWN_NOTE

    def _listing(self) -> str:
        notes = self._notes.everything()
        if not notes:
            return NOTHING
        lines = ["🧠 Notes en mémoire"]
        for scope, about in groupby(notes, key=lambda note: note.scope):
            lines += ["", scope, *(f"{note.id}. {note.text}" for note in about)]
        return "\n".join([*lines, "", USAGE])
