from typing import Protocol, runtime_checkable


class UnsubscribeError(Exception):
    """The unsubscribe request was refused or failed. Its message must never contain the URL."""


@runtime_checkable
class Unsubscriber(Protocol):
    def unsubscribe(self, url: str) -> None:
        """Sends a one-click unsubscribe request (RFC 8058); raises UnsubscribeError."""
        ...
