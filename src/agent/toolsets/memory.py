import json
import re

from src.agent.reply import is_plain_address
from src.agent.schema import arguments, text
from src.agent.tools import Tool, ToolRefused
from src.domain import ToolSpec, canonical_address
from src.ports import MemoryNotes

MAX_NOTE_CHARS = 300
MAX_ADDRESS_CHARS = 200
NOT_AN_ADDRESS = "Give the correspondent's full email address."
NOT_THE_USERS_WORDS = (
    "Only what the user wrote in their message can be remembered, word for word. Quote the "
    "part of their message to keep, or tell them you cannot remember this."
)
FULL = "Too many notes about this correspondent already. The user can remove some with /memory."
REMEMBERED = "Remembered."
_SPACES = re.compile(r"\s+")


def memory_tools(notes: MemoryNotes, said: str) -> list[Tool]:
    """`said` is the user's own message. A note has to be a passage of it: what a mail says,
    or what the model concludes from one, can therefore never be written into memory."""
    spoken = _normalise(said)

    def remember(args: dict) -> str:
        scope = _scope(args["about"])
        note = _SPACES.sub(" ", args["note"]).strip()
        if _normalise(note) not in spoken:
            raise ToolRefused(NOT_THE_USERS_WORDS)
        if notes.add(scope, note) is None:
            raise ToolRefused(FULL)
        return REMEMBERED

    def recall(args: dict) -> str:
        return json.dumps(
            [
                {"note": note.text, "since": note.created_at.date().isoformat()}
                for note in notes.about(_scope(args["about"]))
            ],
            ensure_ascii=False,
        )

    about = text("Email address of the correspondent the note is about.", MAX_ADDRESS_CHARS)
    return [
        Tool(
            ToolSpec(
                "remember",
                "Keeps a note about a correspondent for later conversations, when the user asks "
                "to remember something about them. The note must be a passage of the user's "
                "message, copied word for word.",
                arguments(about=about, note=text("The passage to keep.", MAX_NOTE_CHARS)),
            ),
            remember,
        ),
        Tool(
            ToolSpec(
                "recall",
                "Returns what the user asked to be remembered about a correspondent.",
                arguments(about=about),
            ),
            recall,
        ),
    ]


def _scope(address: str) -> str:
    address = address.strip()
    if not is_plain_address(address):
        raise ToolRefused(NOT_AN_ADDRESS)
    return canonical_address(address)


def _normalise(words: str) -> str:
    return _SPACES.sub(" ", words).strip().casefold()
