from typing import Protocol, runtime_checkable

from src.domain import Button, ChatEvent


@runtime_checkable
class ChatInbox(Protocol):
    @property
    def is_configured(self) -> bool: ...

    @property
    def inbound_authorized(self) -> bool:
        """False when no user is allowed to act, so the listener must not start."""
        ...

    def get_updates(self, offset: int | None) -> tuple[list[ChatEvent], int | None]: ...

    def send_message(
        self, text: str, buttons: list[list[Button]] | None = None, reply_to: int | None = None
    ) -> int: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def clear_buttons(self, message_id: int) -> None: ...
