import base64
import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_SIGNATURE_RE = re.compile(r"\n--\s?\n")


def decode_body(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text)).strip()


def strip_signature(text: str) -> str:
    match = _SIGNATURE_RE.search(text)
    return text[: match.start()].rstrip() if match else text


def truncate_words(text: str, max_words: int = 1000) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def extract_domain(sender: str) -> str:
    if "@" not in sender:
        return ""
    return sender.rsplit("@", 1)[-1].lower()


def clean_body(raw_text: str, is_html: bool) -> str:
    text = strip_html(raw_text) if is_html else raw_text
    text = strip_signature(text)
    return truncate_words(text)
