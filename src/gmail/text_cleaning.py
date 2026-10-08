import base64
import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_SIGNATURE_RE = re.compile(r"\n--\s?\n")
# Gmail writes the attribution on one line, but other clients wrap it, so it may span two.
_ATTRIBUTION_RE = re.compile(
    r"^[ \t]*(?:Le|On)\b[^\n]*(?:\n[^\n]*)?\b(?:a\s+écrit|wrote)\s*:", re.MULTILINE
)
_FORWARDED_RE = re.compile(
    r"^[ \t]*(?:-{2,}\s*(?:Original Message|Message d'origine|Forwarded message|Message transféré)"
    r"|(?:De|From)\s*:[^\n]*\n[ \t]*(?:Envoyé|Sent|Date)\s*:)",
    re.MULTILINE | re.IGNORECASE,
)
_HTML_QUOTE_RE = re.compile(r"<(?:div[^>]*\bgmail_quote\b|blockquote)", re.IGNORECASE)
MAX_BODY_WORDS = 1000
CUT_MARKER = "[…]"


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


def strip_quoted_reply(text: str, is_html: bool = False) -> str:
    """Keeps what the author wrote above the mail they answer or forward."""
    if is_html:
        match = _HTML_QUOTE_RE.search(text)
        text = strip_html(text[: match.start()] if match else text)
    cuts = [m.start() for m in (_ATTRIBUTION_RE.search(text), _FORWARDED_RE.search(text)) if m]
    if cuts:
        text = text[: min(cuts)]
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def truncate_words(text: str, max_words: int = MAX_BODY_WORDS, tail_words: int = 0) -> str:
    """Keeps the first `max_words` words, plus the last `tail_words` after a visible cut."""
    words = text.split()
    if len(words) <= max_words + tail_words:
        return text
    head = " ".join(words[:max_words])
    if not tail_words:
        return head
    return f"{head} {CUT_MARKER} {' '.join(words[-tail_words:])}"


def extract_domain(sender: str) -> str:
    if "@" not in sender:
        return ""
    return sender.rsplit("@", 1)[-1].lower()


def clean_body(raw_text: str, is_html: bool, max_words: int | None = MAX_BODY_WORDS) -> str:
    text = strip_html(raw_text) if is_html else raw_text
    text = strip_signature(text)
    return text if max_words is None else truncate_words(text, max_words)
