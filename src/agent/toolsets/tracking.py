import json
from datetime import timedelta

from src.agent.schema import arguments, text
from src.agent.tools import Tool
from src.domain import WAITING_FOR_THEM, ToolSpec
from src.ports import DecisionStore

MAX_LISTED = 20
MAX_SENDER_CHARS = 200
DECISIONS_WINDOW = timedelta(days=90)


def tracking_tools(store: DecisionStore) -> list[Tool]:
    """What the assistant itself recorded: read only."""

    def list_pending(_: dict) -> str:
        waiting = store.threads.in_state(WAITING_FOR_THEM)
        return json.dumps(
            [
                {
                    "thread_id": thread.thread_id,
                    "to": list(thread.anchor.to + thread.anchor.cc),
                    "subject": thread.anchor.subject,
                    "sent_at": thread.anchor.sent_at.isoformat(),
                    "follow_up_due_at": thread.due_at.isoformat() if thread.due_at else None,
                    "probability_it_expects_an_answer": thread.expects_answer,
                }
                for thread in waiting[:MAX_LISTED]
                if thread.anchor is not None
            ],
            ensure_ascii=False,
        )

    def recall_decisions(args: dict) -> str:
        wanted = args["sender"].strip().lower()
        matching = [
            record
            for record in store.decisions_since(DECISIONS_WINDOW)
            if wanted in record.sender.lower()
        ]
        return json.dumps(
            [
                {
                    "message_id": record.message_id,
                    "thread_id": record.thread_id,
                    "from": record.sender,
                    "subject": record.subject,
                    "date": record.created_at,
                    "urgency": record.urgency,
                    "category": record.category,
                    "route": record.route,
                }
                for record in matching[-MAX_LISTED:]
            ],
            ensure_ascii=False,
        )

    return [
        Tool(
            ToolSpec(
                "list_pending",
                "Lists the mails the user sent that still wait for an answer.",
                arguments(),
            ),
            list_pending,
        ),
        Tool(
            ToolSpec(
                "recall_decisions",
                "Lists how the last mails from a sender were triaged (urgency, category, "
                "route: reject = archived, label = kept, llm = alerted).",
                arguments(sender=text("Address or part of it, e.g. a domain.", MAX_SENDER_CHARS)),
            ),
            recall_decisions,
        ),
    ]
