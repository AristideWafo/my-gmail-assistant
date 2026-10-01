from typing import Protocol, runtime_checkable

from src.domain import EmailMessage


@runtime_checkable
class MailProvider(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def check_connection(self) -> str: ...

    def fetch_unread(self) -> list[EmailMessage]: ...

    def fetch_history(self) -> list[EmailMessage]: ...

    def fetch_message(self, message_id: str) -> EmailMessage | None:
        """Returns None when the message no longer exists or the provider is not configured."""
        ...

    def archive_message(self, message_id: str) -> None: ...

    def label_message(self, message_id: str, label_name: str) -> None: ...

    def create_draft(
        self, thread_id: str, to: str, subject: str, body: str, in_reply_to: str = ""
    ) -> str | None:
        """Returns the draft id, or None when the provider is not configured."""
        ...

    def send_draft(self, draft_id: str) -> bool:
        """Returns False when the provider is not configured; raises when sending failed."""
        ...
