import time
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, ClassVar

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    body: str


class GmailClient:
    SCOPES: ClassVar[list[str]] = ["https://www.googleapis.com/auth/gmail.modify"]

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, user_id: str = "me") -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._user_id = user_id
        self._service = self._build_service()

    def _build_service(self):
        if not (self._client_id and self._client_secret and self._refresh_token):
            return None
        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=self.SCOPES,
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def fetch_unread(self, max_results: int = 10) -> list[EmailMessage]:
        if not self._service:
            return []

        result = (
            self._service.users()
            .messages()
            .list(userId=self._user_id, q="is:unread", maxResults=max_results)
            .execute()
        )
        messages = result.get("messages", [])

        parsed: list[EmailMessage] = []
        for message in messages:
            payload = (
                self._service.users()
                .messages()
                .get(userId=self._user_id, id=message["id"], format="full")
                .execute()
            )
            parsed.append(self._parse_message(payload))

        return parsed

    def fetch_history(self, max_results: int = 100, max_retries: int = 5) -> list[EmailMessage]:
        if not self._service:
            return []

        delay = 1.0
        for _ in range(max_retries):
            try:
                result = (
                    self._service.users()
                    .messages()
                    .list(userId=self._user_id, maxResults=max_results)
                    .execute()
                )
                messages = result.get("messages", [])
                return [
                    self._parse_message(
                        self._service.users()
                        .messages()
                        .get(userId=self._user_id, id=m["id"], format="full")
                        .execute()
                    )
                    for m in messages
                ]
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status == 429:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise
        return []

    def add_label(self, message_id: str, label_id: str) -> None:
        if not self._service:
            return
        (
            self._service.users()
            .messages()
            .modify(
                userId=self._user_id,
                id=message_id,
                body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]},
            )
            .execute()
        )

    def create_draft(self, thread_id: str, to: str, subject: str, body: str) -> dict[str, Any] | None:
        if not self._service:
            return None

        mime_message = f"To: {to}\r\nSubject: Re: {subject}\r\n\r\n{body}"
        payload = {"message": {"raw": mime_message.encode("utf-8").hex(), "threadId": thread_id}}
        return self._service.users().drafts().create(userId=self._user_id, body=payload).execute()

    @staticmethod
    def _parse_message(message: dict[str, Any]) -> EmailMessage:
        headers = {h.get("name", ""): h.get("value", "") for h in message.get("payload", {}).get("headers", [])}
        sender = parseaddr(headers.get("From", ""))[1] or headers.get("From", "")
        subject = headers.get("Subject", "(No subject)")
        snippet = message.get("snippet", "")

        body_data = ""
        payload = message.get("payload", {})
        if payload.get("body", {}).get("data"):
            body_data = payload["body"]["data"]
        elif payload.get("parts"):
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    body_data = part["body"]["data"]
                    break

        return EmailMessage(
            id=message.get("id", ""),
            thread_id=message.get("threadId", ""),
            sender=sender,
            subject=subject,
            snippet=snippet,
            body=body_data,
        )
