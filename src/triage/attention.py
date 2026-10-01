def _yes_no(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


# Three narrow questions instead of a broad "is this worth seeing": each one is stored, measured
# and can be dropped on its own. Wording measured on the live API; the exclusions are what keeps
# advertised webinars, sales deadlines and terms updates out.
ATTENTION_QUESTIONS = {
    "personal_event": _yes_no(
        "Does this email concern an event, meeting, call, appointment or session with a date "
        "that its recipient is personally expected at? Judge dates against `today`.",
        "The recipient is invited by name, registered, booked or reminded of his own "
        "registration or appointment, for a date still to come",
        "No such event: advertising or suggestions for webinars, courses, meetups or conferences "
        "the recipient did not sign up for, a newsletter agenda, a past event, or no event at all",
    ),
    "service_change": _yes_no(
        "Does this email announce a dated interruption or change, still to come, of a service, "
        "tool or utility that its recipient uses? Judge dates against `today`.",
        "A planned outage, cut, maintenance, works, migration, deprecation or removal on a "
        "stated date that will affect the recipient",
        "Nothing of the kind: terms or policy updates needing no action, product news, new "
        "features, promotions, statements, receipts, incident already over, or no date",
    ),
    "personal_deadline": _yes_no(
        "Does this email ask its recipient to do something before a stated date? Judge dates "
        "against `today`.",
        "The recipient personally must provide, sign, pay, renew, confirm or fix something by a "
        "date still to come, or lose a right or a service",
        "No obligation with a date: sales offers ending soon, enrolment or registration "
        "deadlines of things advertised, newsletters, statements, receipts, or a date already "
        "passed",
    ),
}
