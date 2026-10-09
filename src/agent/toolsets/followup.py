from dataclasses import replace

from src.agent.reply import has_unseen_characters
from src.agent.schema import arguments, text
from src.agent.tools import Tool, ToolRefused
from src.agent.toolsets.mail import short
from src.domain import ToolSpec, TrackedThread
from src.formatting import clean_draft, has_placeholder
from src.ports import ThreadStore

MAX_FOLLOW_UP_CHARS = 1200
MAX_REASON_CHARS = 200
ADVICE = ("send", "wait", "drop")
PLACEHOLDER = "The text still holds a placeholder in square brackets. Write the final text."
UNSEEN = "The text holds characters that do not show on screen. Write it in plain characters."
UNKNOWN_ADVICE = f"`advice` must be one of: {', '.join(ADVICE)}."
REASON_NEEDED = "Say in `reason` why you advise not to send it now."
KEPT = "Kept for the user, who decides whether it is sent. Nothing was sent."
THREAD_MOVED = "The thread changed while you wrote: nothing was kept."


def followup_tools(threads: ThreadStore, thread_id: str, anchor_id: str) -> list[Tool]:
    """`write_follow_up` keeps a text on the tracked thread and nothing else: offering it and
    sending it stay with the code that already rechecks the thread and asks the user."""

    def write_follow_up(args: dict) -> str:
        body = clean_draft(args["text"])
        if not body or has_placeholder(body):
            raise ToolRefused(PLACEHOLDER)
        if has_unseen_characters(body):
            raise ToolRefused(UNSEEN)
        advice = args.get("advice", "send")
        if advice not in ADVICE:
            raise ToolRefused(UNKNOWN_ADVICE)
        reason = short(args.get("reason", ""))[:MAX_REASON_CHARS]
        if advice != "send" and not reason:
            raise ToolRefused(REASON_NEEDED)
        if has_unseen_characters(reason):
            raise ToolRefused(UNSEEN)

        def keep(thread: TrackedThread | None) -> TrackedThread | None:
            # Written for one anchor: if I wrote again or the thread was answered meanwhile,
            # the text answers a situation that is gone.
            if thread is None or thread.anchor is None or thread.anchor.message_id != anchor_id:
                return None
            return replace(
                thread,
                composed_text=body,
                composed_for=anchor_id,
                composed_advice="" if advice == "send" else f"{advice}: {reason}",
            )

        return KEPT if threads.update(thread_id, keep) is not None else THREAD_MOVED

    return [
        Tool(
            ToolSpec(
                "write_follow_up",
                "Keeps the follow-up you wrote, for the user to send or not. Call it once, "
                "when the text is final; it ends your turn. If you think it should not be "
                "sent now, still give a text and say so with `advice` and `reason`.",
                arguments(
                    optional=("advice", "reason"),
                    text=text(
                        "The whole follow-up, from greeting to signature, with no placeholder.",
                        MAX_FOLLOW_UP_CHARS,
                    ),
                    advice=text("send (default), wait or drop.", 10),
                    reason=text("Why not send it now, in French, one sentence.", MAX_REASON_CHARS),
                ),
            ),
            write_follow_up,
            ends_run=True,
        )
    ]
