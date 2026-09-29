from typing import Protocol, runtime_checkable

from src.domain import Button


class ChannelDeliveryError(Exception):
    """Raised by a channel when delivery failed. Its message must never contain secrets."""


@runtime_checkable
class AlertChannel(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def is_configured(self) -> bool: ...

    @property
    def interactive(self) -> bool:
        """True when the channel renders buttons and returns a message id that replies can target."""
        ...

    def check_connection(self) -> str: ...

    def send(self, text: str, buttons: list[list[Button]] | None = None) -> int | None:
        """Returns the channel message id when `interactive`, else None; raises ChannelDeliveryError."""
        ...
