import base64
import time
from collections.abc import Callable
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
        self._label_cache: dict[str, str] = {}

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

    def fetch_unread(self, max_results: int = 10, max_retries: int = 5) -> list[EmailMessage]:
        if not self._service:
            return []
        return self._list_and_parse(query="is:unread", max_results=max_results, max_retries=max_retries)

    def fetch_history(self, max_results: int = 100, max_retries: int = 5) -> list[EmailMessage]:
        if not self._service:
            return []
        return self._list_and_parse(query=None, max_results=max_results, max_retries=max_retries)

    def _list_and_parse(self, query: str | None, max_results: int, max_retries: int) -> list[EmailMessage]:
        def list_messages():
            list_kwargs: dict[str, Any] = {"userId": self._user_id, "maxResults": max_results}
            if query:
                list_kwargs["q"] = query
            result = self._service.users().messages().list(**list_kwargs).execute()
            return [
                self._parse_message(
                    self._service.users().messages().get(userId=self._user_id, id=m["id"], format="full").execute()
                )
                for m in result.get("messages", [])
            ]

        return self._execute_with_backoff(list_messages, max_retries=max_retries) or []

    @staticmethod
    def _execute_with_backoff(request_factory: Callable[[], Any], max_retries: int = 5):
        delay = 1.0
        for _ in range(max_retries):
            try:
                return request_factory()
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status == 429:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise
        return None

    def archive_message(self, message_id: str) -> None:
        if not self._service:
            return
        (
            self._service.users()
            .messages()
            .modify(
                userId=self._user_id,
                id=message_id,
                body={"removeLabelIds": ["INBOX", "UNREAD"]},
            )
            .execute()
        )

    def ensure_label(self, name: str) -> str | None:
        if not self._service:
            return None
        if name in self._label_cache:
            return self._label_cache[name]

        existing = self._service.users().labels().list(userId=self._user_id).execute()
        for label in existing.get("labels", []):
            if label.get("name") == name:
                self._label_cache[name] = label["id"]
                return label["id"]

        created = (
            self._service.users()
            .labels()
            .create(userId=self._user_id, body={"name": name, "labelListVisibility": "labelShow"})
            .execute()
        )
        self._label_cache[name] = created["id"]
        return created["id"]

    def label_message(self, message_id: str, label_name: str) -> None:
        if not self._service:
            return
        label_id = self.ensure_label(f"Assistant/{label_name.capitalize()}")
        if not label_id:
            return
        self.add_label(message_id, label_id)

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
        raw = base64.urlsafe_b64encode(mime_message.encode("utf-8")).decode("utf-8")
        payload = {"message": {"raw": raw, "threadId": thread_id}}
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
