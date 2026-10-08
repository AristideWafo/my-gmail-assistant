import base64
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email.mime.text import MIMEText
from email.utils import getaddresses, parseaddr
from typing import Any, ClassVar

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.domain import EmailMessage, ThreadMessage, ThreadRef, ThreadSnapshot
from src.gmail.text_cleaning import (
    MAX_BODY_WORDS,
    clean_body,
    decode_body,
    extract_domain,
    strip_quoted_reply,
    strip_signature,
)

_GMAIL_AUTHSERV_ID = "mx.google.com"
_DMARC_PASS_RE = re.compile(r"\bdmarc=pass\b", re.IGNORECASE)
_THREAD_HEADERS = (
    "From",
    "To",
    "Cc",
    "Subject",
    "Message-ID",
    "Auto-Submitted",
    "Precedence",
    "X-Autoreply",
    "X-Autorespond",
    "List-Id",
)
_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
# Only a bare address may reach a search query: anything else could add search operators.
_PLAIN_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def dmarc_passed(headers: list[dict[str, Any]]) -> bool:
    """Reads Gmail's own verdict, never one the sender could have written."""
    for header in headers:
        if header.get("name", "").lower() != "authentication-results":
            continue
        value = header.get("value", "")
        # Gmail prepends its header, so its verdict is the topmost one carrying its id; any
        # Authentication-Results below it, or under another id, came with the message.
        if value.split(";", 1)[0].strip().lower() == _GMAIL_AUTHSERV_ID:
            return bool(_DMARC_PASS_RE.search(value))
    return False


def build_unread_query(max_age_days: int) -> str:
    return f"is:unread in:inbox newer_than:{max_age_days}d"


class GmailClient:
    SCOPES: ClassVar[list[str]] = ["https://www.googleapis.com/auth/gmail.modify"]

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        user_id: str = "me",
        unread_query: str | None = None,
    ) -> None:
        self._unread_query = unread_query or build_unread_query(3)
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._user_id = user_id
        self._service = self._build_service()
        self._label_cache: dict[str, str] = {}
        self._my_addresses: frozenset[str] | None = None

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

    @property
    def is_configured(self) -> bool:
        return self._service is not None

    def check_connection(self) -> str:
        profile = self._service.users().getProfile(userId=self._user_id).execute()
        return f"authenticated as {profile['emailAddress']}"

    def fetch_unread(self, max_results: int = 10, max_retries: int = 5) -> list[EmailMessage]:
        if not self._service:
            return []
        return self._list_and_parse(query=self._unread_query, max_results=max_results, max_retries=max_retries)

    def fetch_history(self, max_results: int = 100, max_retries: int = 5) -> list[EmailMessage]:
        if not self._service:
            return []
        return self._list_and_parse(query=None, max_results=max_results, max_retries=max_retries)

    def fetch_message(
        self, message_id: str, full_body: bool = False, max_retries: int = 5
    ) -> EmailMessage | None:
        if not self._service:
            return None
        request = self._service.users().messages().get(
            userId=self._user_id, id=message_id, format="full"
        )
        try:
            message = self._execute_with_backoff(request.execute, max_retries=max_retries)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise
        return self._parse_message(message, full_body)

    def in_inbox(self, message_id: str, max_retries: int = 5) -> bool:
        if not self._service:
            return True
        request = self._service.users().messages().get(
            userId=self._user_id, id=message_id, format="minimal"
        )
        try:
            message = self._execute_with_backoff(request.execute, max_retries=max_retries)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return False
            raise
        return "INBOX" in message.get("labelIds", [])

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

        return self._execute_with_backoff(list_messages, max_retries=max_retries)

    @staticmethod
    def _execute_with_backoff(request_factory: Callable[[], Any], max_retries: int = 5):
        delay = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                return request_factory()
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                # Giving up must raise: an empty result would read as "no unread mail".
                if status != 429 or attempt == max_retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise ValueError(f"max_retries must be at least 1, got {max_retries}")

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

    def label_message(self, message_id: str, *label_names: str) -> None:
        if not self._service:
            return
        label_ids = [self.ensure_label(f"Assistant/{name.capitalize()}") for name in label_names]
        (
            self._service.users()
            .messages()
            .modify(
                userId=self._user_id,
                id=message_id,
                body={"addLabelIds": label_ids, "removeLabelIds": ["UNREAD"]},
            )
            .execute()
        )

    def create_draft(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        cc: Sequence[str] = (),
        references: Sequence[str] = (),
    ) -> str | None:
        if not self._service:
            return None

        mime_message = MIMEText(body, "plain", "utf-8")
        mime_message["To"] = to
        if cc:
            mime_message["Cc"] = ", ".join(cc)
        mime_message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if in_reply_to:
            # threadId only threads on Gmail's side; other clients rely on these RFC 5322 headers.
            mime_message["In-Reply-To"] = in_reply_to
            mime_message["References"] = " ".join(references) or in_reply_to
        raw = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")
        payload = {"message": {"raw": raw, "threadId": thread_id}}
        draft = self._service.users().drafts().create(userId=self._user_id, body=payload).execute()
        return draft.get("id")

    def send_draft(self, draft_id: str) -> bool:
        if not self._service:
            return False
        self._service.users().drafts().send(userId=self._user_id, body={"id": draft_id}).execute()
        return True

    def my_addresses(self, max_retries: int = 5) -> frozenset[str]:
        if not self._service:
            return frozenset()
        if self._my_addresses is None:
            request = self._service.users().settings().sendAs().list(userId=self._user_id)
            aliases = self._execute_with_backoff(request.execute, max_retries=max_retries)
            self._my_addresses = frozenset(
                alias["sendAsEmail"].lower()
                for alias in aliases.get("sendAs", [])
                if alias.get("sendAsEmail")
            )
        return self._my_addresses

    def sent_threads(self, newer_than_days: int, limit: int, max_retries: int = 5) -> list[ThreadRef]:
        if not self._service:
            return []
        request = self._service.users().threads().list(
            userId=self._user_id, q=f"in:sent newer_than:{newer_than_days}d", maxResults=limit
        )
        result = self._execute_with_backoff(request.execute, max_retries=max_retries)
        return [
            ThreadRef(thread["id"], str(thread.get("historyId", "")))
            for thread in result.get("threads", [])
        ]

    def thread_snapshot(self, thread_id: str, max_retries: int = 5) -> ThreadSnapshot | None:
        if not self._service:
            return None
        request = self._service.users().threads().get(
            userId=self._user_id,
            id=thread_id,
            format="metadata",
            metadataHeaders=list(_THREAD_HEADERS),
        )
        try:
            thread = self._execute_with_backoff(request.execute, max_retries=max_retries)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise
        mine = self.my_addresses(max_retries=max_retries)
        return ThreadSnapshot(
            thread_id=thread.get("id", thread_id),
            history_id=str(thread.get("historyId", "")),
            messages=tuple(
                self._parse_thread_message(message, mine) for message in thread.get("messages", [])
            ),
        )

    def sent_text(self, message_id: str, max_retries: int = 5) -> str:
        if not self._service:
            return ""
        request = self._service.users().messages().get(
            userId=self._user_id, id=message_id, format="full"
        )
        message = self._execute_with_backoff(request.execute, max_retries=max_retries)
        body_data, is_html = self._extract_body(message.get("payload", {}))
        return strip_signature(strip_quoted_reply(decode_body(body_data), is_html))

    def has_message_from(self, address: str, after: datetime, max_retries: int = 5) -> bool:
        if not self._service or not _PLAIN_ADDRESS_RE.match(address):
            return True
        request = self._service.users().messages().list(
            userId=self._user_id,
            # in:anywhere: an answer filed as spam or deleted is still an answer.
            q=f"from:{address} after:{int(after.timestamp())} in:anywhere",
            maxResults=1,
        )
        result = self._execute_with_backoff(request.execute, max_retries=max_retries)
        return bool(result.get("messages"))

    @staticmethod
    def _parse_thread_message(message: dict[str, Any], mine: frozenset[str]) -> ThreadMessage:
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in message.get("payload", {}).get("headers", [])
        }
        sender = (parseaddr(headers.get("from", ""))[1] or headers.get("from", "")).lower()
        return ThreadMessage(
            id=message.get("id", ""),
            sender=sender,
            to=_addresses(headers.get("to", "")),
            cc=_addresses(headers.get("cc", "")),
            sent_at=_internal_datetime(message),
            subject=headers.get("subject", ""),
            message_id_header=headers.get("message-id", ""),
            from_me="SENT" in message.get("labelIds", []) or sender in mine,
            automated=_is_automated(headers, sender),
            bounce=sender.startswith(_BOUNCE_SENDERS),
        )

    @staticmethod
    def _extract_body(payload: dict[str, Any]) -> tuple[str, bool]:
        top_mime = payload.get("mimeType", "")
        if payload.get("body", {}).get("data"):
            return payload["body"]["data"], top_mime == "text/html"

        html_data = ""
        for part in payload.get("parts", []):
            mime_type = part.get("mimeType")
            data = part.get("body", {}).get("data")
            if not data:
                continue
            if mime_type == "text/plain":
                return data, False
            if mime_type == "text/html" and not html_data:
                html_data = data

        return html_data, True

    @classmethod
    def _parse_message(cls, message: dict[str, Any], full_body: bool = False) -> EmailMessage:
        header_list = message.get("payload", {}).get("headers", [])
        headers = {h.get("name", "").lower(): h.get("value", "") for h in header_list}
        sender = parseaddr(headers.get("from", ""))[1] or headers.get("from", "")
        subject = headers.get("subject", "(No subject)")
        snippet = message.get("snippet", "")

        body_data, is_html = cls._extract_body(message.get("payload", {}))
        body = clean_body(
            decode_body(body_data), is_html, max_words=None if full_body else MAX_BODY_WORDS
        )

        return EmailMessage(
            id=message.get("id", ""),
            thread_id=message.get("threadId", ""),
            sender=sender,
            subject=subject,
            snippet=snippet,
            body=body,
            sender_domain=extract_domain(sender),
            received_at=cls._received_at(message),
            message_id_header=headers.get("message-id", ""),
            dmarc_pass=dmarc_passed(header_list),
            list_unsubscribe=headers.get("list-unsubscribe", ""),
            list_unsubscribe_post=headers.get("list-unsubscribe-post", ""),
        )

    @staticmethod
    def _received_at(message: dict[str, Any]) -> str:
        try:
            return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC).date().isoformat()
        except (KeyError, ValueError, TypeError):
            return ""


def _addresses(header: str) -> tuple[str, ...]:
    return tuple(address.lower() for _, address in getaddresses([header]) if address)


def _internal_datetime(message: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)
    except (KeyError, ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=UTC)


def _is_automated(headers: dict[str, str], sender: str) -> bool:
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    precedence = headers.get("precedence", "").strip().lower()
    return (
        (bool(auto_submitted) and auto_submitted != "no")
        or precedence in ("bulk", "auto_reply", "list", "junk")
        or bool(headers.get("x-autoreply") or headers.get("x-autorespond"))
        or bool(headers.get("list-id"))
        or sender.startswith(_BOUNCE_SENDERS)
    )
