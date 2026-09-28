import re

_RULE_RE = re.compile(r"^\s*(\*{3,}|-{3,}|_{3,})\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+•][ \t]+", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_PREAMBLE_RE = re.compile(r"^(voici|here is|here's|here are|ci-dessous|proposition de r[ée]ponse)\b.*:$", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]+\]")
_SUBJECT_LINE_RE = re.compile(r"^(objet|subject|sujet)\s*:", re.IGNORECASE)


def strip_markdown(text: str, bullet: str = "• ") -> str:
    text = _RULE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = _BOLD_RE.sub(lambda match: match.group(1) or match.group(2), text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub(bullet, text)
    text = text.replace("`", "")
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def clean_draft(text: str) -> str:
    """LLMs prepend a chat-style preamble and echo the subject; neither belongs in a mail body."""
    lines = strip_markdown(text, bullet="- ").splitlines()
    while lines and _is_draft_noise(lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()


def _is_draft_noise(line: str) -> bool:
    stripped = line.strip()
    return not stripped or bool(_PREAMBLE_RE.match(stripped) or _SUBJECT_LINE_RE.match(stripped))


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text))
