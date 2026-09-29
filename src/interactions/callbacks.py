from dataclasses import dataclass

from src.gateways.telegram_bot import Button, is_valid_callback_data

FEEDBACK = "fb"
SEND = "send"
CANCEL = "cancel"

VERDICT_CODES = {"v": "valid", "u": "false_urgent", "s": "false_spam"}
_FEEDBACK_LABELS = (("v", "Valider"), ("u", "Faux-Urgent"), ("s", "Faux-Spam"))


@dataclass(frozen=True)
class Callback:
    action: str
    target: str
    verdict: str = ""


def feedback_buttons(gmail_id: str) -> list[list[Button]] | None:
    return _single_row(
        [(label, f"{FEEDBACK}:{code}:{gmail_id}") for code, label in _FEEDBACK_LABELS]
    )


def draft_buttons(draft_id: str) -> list[list[Button]] | None:
    return _single_row([("Envoyer", f"{SEND}:{draft_id}"), ("Annuler", f"{CANCEL}:{draft_id}")])


def parse_callback(data: str) -> Callback | None:
    action, _, rest = data.partition(":")
    if action == FEEDBACK:
        code, _, gmail_id = rest.partition(":")
        verdict = VERDICT_CODES.get(code)
        return Callback(FEEDBACK, gmail_id, verdict) if verdict and gmail_id else None
    if action in (SEND, CANCEL) and rest:
        return Callback(action, rest)
    return None


def _single_row(row: list[Button]) -> list[list[Button]] | None:
    # Telegram rejects the whole sendMessage when any callback_data exceeds 64 bytes, which
    # would lose the message itself; better to send it without buttons.
    if all(is_valid_callback_data(data) for _, data in row):
        return [row]
    return None
