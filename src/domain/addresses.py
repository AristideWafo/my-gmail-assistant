_GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def canonical_address(address: str) -> str:
    """One spelling per mailbox: the +tag and, at Gmail, the dots do not change who receives it."""
    local, separator, domain = address.strip().lower().rpartition("@")
    if not separator:
        return address.strip().lower()
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"
