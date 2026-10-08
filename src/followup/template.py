import re
from datetime import tzinfo

from src.domain import FollowUpAnchor

_ENGLISH_WORDS = re.compile(
    r"\b(the|you|your|i|me|my|to|of|for|on|in|this|that|it|please|could|would|can|any|update|"
    r"see|attached|let|know|thanks|thank|regards|hi|hello|and|is|are|will|do|did)\b",
    re.IGNORECASE,
)
_FRENCH_WORDS = re.compile(
    r"\b(le|la|les|un|une|des|du|de|vous|tu|te|toi|merci|bonjour|salut|pour|est|et|je|j|"
    r"me|mon|ma|pouvez|pourriez|peux|cordialement|voici|ci|joint|avec|sur|dans|pas)\b",
    re.IGNORECASE,
)
_TU_WORDS = re.compile(r"\b(tu|te|toi|ton|ta|tes|t)\b", re.IGNORECASE)
_VOUS_WORDS = re.compile(r"\b(vous|votre|vos)\b", re.IGNORECASE)
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def is_english(text: str, subject: str = "") -> bool:
    """The language of my own mail: its text first, its subject when the text says nothing,
    French when neither does."""
    for sample in (text, subject):
        english, french = len(_ENGLISH_WORDS.findall(sample)), len(_FRENCH_WORDS.findall(sample))
        if english != french:
            return english > french
    return False


def uses_tu(text: str) -> bool:
    return bool(_TU_WORDS.search(text)) and not _VOUS_WORDS.search(text)


def follow_up_text(anchor: FollowUpAnchor, my_text: str, signature: str, timezone: tzinfo) -> str:
    """A fixed, polite reminder: no name guessed from an address, no new commitment, nothing
    taken from what the other side wrote."""
    sent = anchor.sent_at.astimezone(timezone)
    subject = _bare_subject(anchor.subject)
    if is_english(my_text, subject):
        about = f' about "{subject}"' if subject else ""
        lines = [
            "Hello,",
            "",
            f"I am following up on my message of {_MONTHS[sent.month - 1]} {sent.day}{about}.",
            "",
            "Best regards,",
        ]
    else:
        about = f" concernant « {subject} »" if subject else ""
        you, closing = ("toi", "Bonne journée,") if uses_tu(my_text) else ("vous", "Bien cordialement,")
        lines = [
            "Bonjour,",
            "",
            (
                f"Je me permets de revenir vers {you} au sujet de mon message du "
                f"{sent.strftime('%d/%m')}{about}."
            ),
            "",
            closing,
        ]
    if signature:
        lines.append(signature)
    return "\n".join(lines)


def _bare_subject(subject: str) -> str:
    return re.sub(r"^\s*((re|tr|fwd?)\s*:\s*)+", "", subject, flags=re.IGNORECASE).strip()
