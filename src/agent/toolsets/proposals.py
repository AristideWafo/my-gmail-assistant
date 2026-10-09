import logging
from collections.abc import Callable
from dataclasses import dataclass

from src.agent.reply import (
    MAX_PREVIEW_CHARS,
    MAX_REPLY_CHARS,
    PROPOSAL_LIFETIME,
    SEND_REPLY,
    format_proposal,
    has_unseen_characters,
    reply_payload,
    reply_target,
    shown_length,
)
from src.agent.schema import arguments, text
from src.agent.tools import Tool, ToolRefused
from src.agent.toolsets.mail import MAX_ID_CHARS
from src.domain import Button, ToolSpec
from src.formatting import clean_draft, has_placeholder
from src.ports import ChatInbox, DecisionStore, MailProvider

logger = logging.getLogger(__name__)

NO_ONE_TO_ANSWER = "This thread has nobody to answer: no such thread, or only automated senders."
PLACEHOLDER = "The text still holds a placeholder in square brackets. Write the final text."
UNSEEN = "The text holds characters that do not show on screen. Write it in plain characters."
ALREADY_PROPOSED = "A reply was already shown to the user in this run."
OTHER_THREAD = "The reply being rewritten is in another thread: propose in that one."
TOO_LONG = "The text is too long to be shown whole for confirmation. Shorten it."
PROPOSED = "Shown to the user, who decides whether it is sent. Nothing was sent."


@dataclass(frozen=True)
class Revised:
    """The proposal a run was asked to rewrite."""

    action_id: str
    chat_message_id: int
    thread_id: str


def proposal_tools(
    mail: MailProvider,
    store: DecisionStore,
    chat: ChatInbox,
    buttons: Callable[[str], list[list[Button]] | None],
    reply_to: int | None,
    revised: Revised | None = None,
) -> list[Tool]:
    """`propose_reply` writes nothing to the mailbox: it stores what would be sent and shows
    it. The model gives the text; whom it goes to is read from the thread.

    A run that rewrites a proposal may only propose in the same thread, and the proposal it
    replaces is withdrawn once the new one is on screen."""
    proposed = False

    def propose_reply(args: dict) -> str:
        nonlocal proposed
        if proposed:
            raise ToolRefused(ALREADY_PROPOSED)
        if revised is not None and args["thread_id"] != revised.thread_id:
            raise ToolRefused(OTHER_THREAD)
        snapshot = mail.thread_snapshot(args["thread_id"])
        target = None if snapshot is None else reply_target(snapshot, mail.my_addresses())
        if target is None:
            raise ToolRefused(NO_ONE_TO_ANSWER)
        body = clean_draft(args["body"])
        if not body or has_placeholder(body):
            raise ToolRefused(PLACEHOLDER)
        if has_unseen_characters(body):
            raise ToolRefused(UNSEEN)
        payload = reply_payload(snapshot.thread_id, target, body)
        preview = format_proposal(payload)
        if shown_length(preview) > MAX_PREVIEW_CHARS:
            raise ToolRefused(TOO_LONG)
        action = store.pending_actions.propose(SEND_REPLY, payload, PROPOSAL_LIFETIME)
        # An action never bound to a message can never be carried out: a failure here leaves
        # nothing to clean up.
        message_id = chat.send_message(preview, buttons=buttons(action.id), reply_to=reply_to)
        store.pending_actions.attach_chat_message(action.id, message_id)
        proposed = True
        if revised is not None:
            _withdraw(store, chat, revised.action_id, revised.chat_message_id)
        return PROPOSED

    return [
        Tool(
            ToolSpec(
                "propose_reply",
                "Shows the user a reply to a thread, for them to send or not. Give the thread "
                "and the full text of the reply only: the recipients and the subject are taken "
                "from the thread. Call it once, when the text is final; it ends your turn.",
                arguments(
                    thread_id=text("Id of the thread to answer in.", MAX_ID_CHARS),
                    body=text(
                        "The whole reply, from greeting to signature, in the language of the "
                        "thread, with no placeholder.",
                        MAX_REPLY_CHARS,
                    ),
                ),
            ),
            propose_reply,
            ends_run=True,
        )
    ]


def _withdraw(store: DecisionStore, chat: ChatInbox, action_id: str, message_id: int) -> None:
    if not store.pending_actions.cancel(action_id, message_id):
        return
    try:
        chat.clear_buttons(message_id)
    except Exception as exc:  # noqa: BLE001 - a press on the old buttons is refused anyway
        logger.warning("Could not withdraw proposal %s: %s", message_id, exc)
