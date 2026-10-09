import json
from collections.abc import Callable
from datetime import datetime

from src.agent.schema import arguments, text
from src.agent.scope import MailScope
from src.agent.tools import Tool, ToolRefused
from src.agent.toolsets.mail import MAX_BODY_CHARS, MAX_ID_CHARS, short
from src.domain import ToolSpec
from src.formatting import truncate
from src.ports import MailProvider, QuestionJudge

MAX_QUESTIONS_PER_RUN = 5
MAX_QUESTION_CHARS = 300
TOO_MANY_QUESTIONS = f"No more than {MAX_QUESTIONS_PER_RUN} questions to JEV in one run."


def judgment_tools(
    judge: QuestionJudge | None,
    mail: MailProvider,
    scope: MailScope,
    clock: Callable[[], datetime],
) -> list[Tool]:
    """`ask_jev`: a fast yes/no judgment on a mail the run may already read. JEV reads the same
    text as the model, so its answer is advice to it and unlocks nothing."""
    if judge is None or not judge.is_configured:
        return []
    asked = 0

    def ask_jev(args: dict) -> str:
        nonlocal asked
        if asked >= MAX_QUESTIONS_PER_RUN:
            raise ToolRefused(TOO_MANY_QUESTIONS)
        email = scope.read(mail, args["message_id"])
        asked += 1
        probability = judge.probability(
            {
                "subject": short(email.subject),
                "body": truncate(email.body or email.snippet, MAX_BODY_CHARS),
                "sender": short(email.sender),
                "received_at": short(email.received_at),
                "today": clock().date().isoformat(),
            },
            args["question"],
            args["yes_means"],
            args["no_means"],
        )
        return json.dumps({"probability_yes": round(probability, 2)})

    return [
        Tool(
            ToolSpec(
                "ask_jev",
                "Asks JEV, a fast classifier, one yes/no question about one mail and returns "
                "the probability of yes. Use it for a closed question (does this mail ask for "
                "an answer? is this date still to come?) instead of reasoning it out.",
                arguments(
                    message_id=text("Id of the mail to judge.", MAX_ID_CHARS),
                    question=text(
                        "The yes/no question, in English, about `subject`, `body`, `sender`, "
                        "`received_at` or `today`.",
                        MAX_QUESTION_CHARS,
                    ),
                    yes_means=text("What makes the answer yes.", MAX_QUESTION_CHARS),
                    no_means=text("What makes the answer no.", MAX_QUESTION_CHARS),
                ),
            ),
            ask_jev,
        )
    ]
