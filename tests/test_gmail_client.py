import base64
import unittest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from src.gmail.client import GmailClient


class FakeResponse:
    def __init__(self, status: int):
        self.status = status
        self.reason = "error"


def make_client_with_service() -> tuple[GmailClient, MagicMock]:
    client = GmailClient(client_id="", client_secret="", refresh_token="")
    service = MagicMock()
    client._service = service
    return client, service


class GmailClientActionsTests(unittest.TestCase):
    def test_archive_message_removes_inbox_and_unread(self):
        client, service = make_client_with_service()

        client.archive_message("msg-1")

        service.users().messages().modify.assert_called_with(
            userId="me",
            id="msg-1",
            body={"removeLabelIds": ["INBOX", "UNREAD"]},
        )

    def test_label_message_creates_label_when_missing_and_caches_it(self):
        client, service = make_client_with_service()
        service.users().labels().list().execute.return_value = {"labels": []}
        service.users().labels().create().execute.return_value = {"id": "label-123"}

        client.label_message("msg-1", "offer")
        client.label_message("msg-2", "offer")

        service.users().labels().create.assert_called_with(
            userId="me",
            body={"name": "Assistant/Offer", "labelListVisibility": "labelShow"},
        )
        self.assertEqual(service.users().labels().create().execute.call_count, 1)
        service.users().messages().modify.assert_called_with(
            userId="me",
            id="msg-2",
            body={"addLabelIds": ["label-123"], "removeLabelIds": ["UNREAD"]},
        )

    def test_label_message_reuses_existing_label(self):
        client, service = make_client_with_service()
        service.users().labels().list().execute.return_value = {
            "labels": [{"name": "Assistant/Urgent", "id": "label-existing"}]
        }

        client.label_message("msg-1", "urgent")

        service.users().labels().create.assert_not_called()
        service.users().messages().modify.assert_called_with(
            userId="me",
            id="msg-1",
            body={"addLabelIds": ["label-existing"], "removeLabelIds": ["UNREAD"]},
        )

    def test_create_draft_encodes_raw_message_as_base64url(self):
        client, service = make_client_with_service()
        service.users().drafts().create().execute.return_value = {"id": "draft-1"}

        client.create_draft("thread-1", "to@example.com", "Subject", "Body text")

        _, kwargs = service.users().drafts().create.call_args
        raw = kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8")
        self.assertIn("To: to@example.com", decoded)
        self.assertIn("Body text", decoded)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


class GmailClientParsingTests(unittest.TestCase):
    def test_parse_message_decodes_and_cleans_plain_body(self):
        message = {
            "id": "1",
            "threadId": "t1",
            "snippet": "hi",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Jane Doe <jane@example.com>"},
                    {"name": "Subject", "value": "Hello"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("Hi there\n-- \nJane Doe, CEO")},
            },
        }

        email = GmailClient._parse_message(message)

        self.assertEqual(email.body, "Hi there")
        self.assertEqual(email.sender_domain, "example.com")

    def test_parse_message_falls_back_to_html_part_and_strips_tags(self):
        message = {
            "id": "2",
            "threadId": "t2",
            "snippet": "hi",
            "payload": {
                "headers": [{"name": "From", "value": "jane@example.com"}],
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64("<p>Hi &amp; welcome</p>")}},
                ],
            },
        }

        email = GmailClient._parse_message(message)

        self.assertEqual(email.body, "Hi & welcome")


class GmailClientBackoffTests(unittest.TestCase):
    def test_fetch_unread_retries_on_429_then_succeeds(self):
        client, service = make_client_with_service()

        call_count = {"n": 0}

        def flaky_execute():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise HttpError(resp=FakeResponse(429), content=b"rate limited")
            return {"messages": []}

        service.users().messages().list().execute.side_effect = flaky_execute

        with patch("time.sleep"):
            result = client.fetch_unread(max_retries=3)

        self.assertEqual(result, [])
        self.assertEqual(call_count["n"], 2)

    def test_fetch_unread_reraises_non_429_errors(self):
        client, service = make_client_with_service()
        service.users().messages().list().execute.side_effect = HttpError(
            resp=FakeResponse(500), content=b"server error"
        )

        with self.assertRaises(HttpError):
            client.fetch_unread(max_retries=3)


if __name__ == "__main__":
    unittest.main()
