from typing import Protocol, runtime_checkable

from src.domain import EmailMessage


@runtime_checkable
class MailProvider(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def check_connection(self) -> str: ...

    def fetch_unread(self) -> list[EmailMessage]: ...

    def fetch_history(self) -> list[EmailMessage]: ...

    def fetch_message(self, message_id: str, full_body: bool = False) -> EmailMessage | None:
        """Returns None when the message no longer exists or the provider is not configured.

        `full_body` returns the cleaned body uncut, instead of the length triage works on.
        """
        ...

    def in_inbox(self, message_id: str) -> bool:
        """False once the message was archived or deleted; True when the provider cannot tell."""
        ...

    def archive_message(self, message_id: str) -> None: ...

    def label_message(self, message_id: str, *label_names: str) -> None:
        """Adds every label and marks the mail as handled, in a single change.

        One call per mail: handling it is what stops it from being fetched again, so labels
        added in separate calls would leave a mail half-labeled and never retried.
        """
        ...

    def create_draft(
        self, thread_id: str, to: str, subject: str, body: str, in_reply_to: str = ""
    ) -> str | None:
        """Returns the draft id, or None when the provider is not configured."""
        ...

    def send_draft(self, draft_id: str) -> bool:
        """Returns False when the provider is not configured; raises when sending failed."""
        ...
