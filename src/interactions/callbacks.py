from dataclasses import dataclass
from typing import Any

from src.domain import Button

FEEDBACK = "fb"
REVIEW = "rv"
PUT_FORWARD = "pf"
SEND = "send"
CANCEL = "cancel"
UNSUBSCRIBE = "unsub"
KEEP = "keep"
FOLLOW_UP = "fu"
FOLLOW_UP_ACTION = "fa"
PROPOSAL = "pa"
CALLBACK_DATA_MAX_BYTES = 64

VERDICT_CODES = {
    "v": "valid",
    "u": "false_urgent",
    "s": "false_spam",
    "m": "missed_urgent",
    "k": "wrong_archive",
    "i": "missed_important",
    "n": "false_important",
}
_FEEDBACK_LABELS = (("v", "Valider"), ("u", "Faux-Urgent"), ("s", "Faux-Spam"))
# What can be wrong depends on what was done: an archived mail may have deserved to stay, a
# labeled one may be spam. Either may have deserved more: "À voir" asks for it to be put forward
# without a notification, "Urgent raté" for an immediate alert.
_REVIEW_LABELS = {
    "label": (("v", "OK"), ("i", "À voir"), ("m", "Urgent raté"), ("s", "Spam")),
    "reject": (("v", "OK"), ("k", "À garder"), ("i", "À voir"), ("m", "Urgent raté")),
}


_PUT_FORWARD_LABELS = (("v", "Vu"), ("n", "Pas utile"))
# Verdicts on a follow-up, not on a mail: they rate a thread, kept apart from VERDICT_CODES.
FOLLOW_UP_VERDICTS = {"u": "useful", "n": "not_useful"}
_FOLLOW_UP_LABELS = (("u", "Relance utile"), ("n", "Pas de relance"))
FOLLOW_UP_ACTIONS = {"s": "send", "z": "snooze", "d": "dismiss", "e": "edit"}
_OFFER_ROWS = (
    (("s", "Envoyer"), ("z", "Reporter 3 j")),
    (("d", "Ne pas relancer"), ("e", "Modifier dans Gmail")),
)
PROPOSAL_ACTIONS = {"s": "send", "c": "cancel"}
_PROPOSAL_LABELS = (("s", "Envoyer"), ("c", "Annuler"))
# Callback data comes from the client: a button set only accepts the verdicts it offers.
_VERDICT_CODES_BY_ACTION = {
    FEEDBACK: {code for code, _ in _FEEDBACK_LABELS},
    REVIEW: {code for labels in _REVIEW_LABELS.values() for code, _ in labels},
    PUT_FORWARD: {code for code, _ in _PUT_FORWARD_LABELS},
}


_CODES_BY_ACTION = {
    FOLLOW_UP: FOLLOW_UP_VERDICTS,
    FOLLOW_UP_ACTION: FOLLOW_UP_ACTIONS,
    PROPOSAL: PROPOSAL_ACTIONS,
}


@dataclass(frozen=True)
class Callback:
    action: str
    target: str
    verdict: str = ""


def feedback_buttons(gmail_id: str) -> list[list[Button]] | None:
    return _single_row(
        [(label, f"{FEEDBACK}:{code}:{gmail_id}") for code, label in _FEEDBACK_LABELS]
    )


def review_buttons(gmail_id: str, route: str) -> list[list[Button]] | None:
    labels = _REVIEW_LABELS.get(route)
    if labels is None:
        return None
    return _single_row([(label, f"{REVIEW}:{code}:{gmail_id}") for code, label in labels])


def put_forward_buttons(gmail_id: str) -> list[list[Button]] | None:
    return _single_row(
        [(label, f"{PUT_FORWARD}:{code}:{gmail_id}") for code, label in _PUT_FORWARD_LABELS]
    )


def follow_up_buttons(thread_id: str) -> list[list[Button]] | None:
    return _single_row(
        [(label, f"{FOLLOW_UP}:{code}:{thread_id}") for code, label in _FOLLOW_UP_LABELS]
    )


def follow_up_offer_buttons(thread_id: str) -> list[list[Button]] | None:
    rows = [
        [(label, f"{FOLLOW_UP_ACTION}:{code}:{thread_id}") for code, label in row]
        for row in _OFFER_ROWS
    ]
    if all(is_valid_callback_data(data) for row in rows for _, data in row):
        return rows
    return None


def proposal_buttons(action_id: str) -> list[list[Button]] | None:
    return _single_row(
        [(label, f"{PROPOSAL}:{code}:{action_id}") for code, label in _PROPOSAL_LABELS]
    )


def draft_buttons(draft_id: str) -> list[list[Button]] | None:
    return _single_row([("Envoyer", f"{SEND}:{draft_id}"), ("Annuler", f"{CANCEL}:{draft_id}")])


def unsubscribe_buttons(offer_id: str) -> list[list[Button]] | None:
    return _single_row(
        [("Se désabonner", f"{UNSUBSCRIBE}:{offer_id}"), ("Garder", f"{KEEP}:{offer_id}")]
    )


def is_valid_callback_data(data: Any) -> bool:
    return isinstance(data, str) and 0 < len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES


def parse_callback(data: str) -> Callback | None:
    action, _, rest = data.partition(":")
    if action in _VERDICT_CODES_BY_ACTION:
        code, _, gmail_id = rest.partition(":")
        if code not in _VERDICT_CODES_BY_ACTION[action] or not gmail_id:
            return None
        return Callback(action, gmail_id, VERDICT_CODES[code])
    if action in _CODES_BY_ACTION:
        codes = _CODES_BY_ACTION[action]
        code, _, thread_id = rest.partition(":")
        if code not in codes or not thread_id:
            return None
        return Callback(action, thread_id, codes[code])
    if action in (SEND, CANCEL, UNSUBSCRIBE, KEEP) and rest:
        return Callback(action, rest)
    return None


def _single_row(row: list[Button]) -> list[list[Button]] | None:
    # Telegram rejects the whole sendMessage when any callback_data exceeds 64 bytes, which
    # would lose the message itself; better to send it without buttons.
    if all(is_valid_callback_data(data) for _, data in row):
        return [row]
    return None
