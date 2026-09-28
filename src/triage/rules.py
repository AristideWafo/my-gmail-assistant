import re

_AUTOMATED_LOCAL_PART_RE = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|notification|newsletter|mailer-daemon|bounce|alerts?$)", re.IGNORECASE
)


def is_automated_sender(sender: str) -> bool:
    local_part = sender.split("@", 1)[0]
    return bool(_AUTOMATED_LOCAL_PART_RE.search(local_part))
