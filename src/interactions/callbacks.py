from dataclasses import dataclass
from typing import Any

from src.domain import Button

FEEDBACK = "fb"
REVIEW = "rv"
SEND = "send"
CANCEL = "cancel"
UNSUBSCRIBE = "unsub"
KEEP = "keep"
CALLBACK_DATA_MAX_BYTES = 64

VERDICT_CODES = {
    "v": "valid",
    "u": "false_urgent",
    "s": "false_spam",
    "m": "missed_urgent",
    "k": "wrong_archive",
    "i": "missed_important",
}
_FEEDBACK_LABELS = (("v", "Valider"), ("u", "Faux-Urgent"), ("s", "Faux-Spam"))
# What can be wrong depends on what was done: an archived mail may have deserved to stay, a
# labeled one may be spam. Either may have deserved more: "À voir" asks for it to be put forward
# without a notification, "Urgent raté" for an immediate alert.
_REVIEW_LABELS = {
    "label": (("v", "OK"), ("i", "À voir"), ("m", "Urgent raté"), ("s", "Spam")),
    "reject": (("v", "OK"), ("k", "À garder"), ("i", "À voir"), ("m", "Urgent raté")),
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
    if action in (FEEDBACK, REVIEW):
        code, _, gmail_id = rest.partition(":")
        verdict = VERDICT_CODES.get(code)
        return Callback(action, gmail_id, verdict) if verdict and gmail_id else None
    if action in (SEND, CANCEL, UNSUBSCRIBE, KEEP) and rest:
        return Callback(action, rest)
    return None


def _single_row(row: list[Button]) -> list[list[Button]] | None:
    # Telegram rejects the whole sendMessage when any callback_data exceeds 64 bytes, which
    # would lose the message itself; better to send it without buttons.
    if all(is_valid_callback_data(data) for _, data in row):
        return [row]
    return None
