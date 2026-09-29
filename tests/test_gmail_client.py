import base64
import unittest
from email import message_from_bytes
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from src.gmail.client import GmailClient, build_unread_query


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

    def create_and_parse_draft(self, subject="Subject", body="Body text", **kwargs):
        client, service = make_client_with_service()
        service.users().drafts().create().execute.return_value = {"id": "draft-1"}

        client.create_draft("thread-1", "to@example.com", subject, body, **kwargs)

        _, kwargs = service.users().drafts().create.call_args
        self.assertEqual(kwargs["body"]["message"]["threadId"], "thread-1")
        return message_from_bytes(base64.urlsafe_b64decode(kwargs["body"]["message"]["raw"].encode("utf-8")))

    def test_create_draft_encodes_raw_message_as_base64url(self):
        message = self.create_and_parse_draft()

        self.assertEqual(message["To"], "to@example.com")
        self.assertEqual(message.get_payload(decode=True).decode("utf-8"), "Body text")

    def test_create_draft_keeps_accents_and_puts_the_subject_only_in_the_header(self):
        message = self.create_and_parse_draft(subject="Rencontre demain", body="Bonjour Céline,\n\nÀ demain")

        self.assertEqual(message["Subject"], "Re: Rencontre demain")
        body = message.get_payload(decode=True).decode("utf-8")
        self.assertEqual(body, "Bonjour Céline,\n\nÀ demain")
        self.assertNotIn("Rencontre demain", body)

    def test_create_draft_does_not_stack_re_prefixes(self):
        self.assertEqual(self.create_and_parse_draft(subject="RE: Hello")["Subject"], "RE: Hello")

    def test_create_draft_sets_threading_headers_when_replying_to_a_message_id(self):
        message = self.create_and_parse_draft(in_reply_to="<abc@mail.example.com>")

        self.assertEqual(message["In-Reply-To"], "<abc@mail.example.com>")
        self.assertEqual(message["References"], "<abc@mail.example.com>")

    def test_create_draft_omits_threading_headers_without_message_id(self):
        message = self.create_and_parse_draft()

        self.assertIsNone(message["In-Reply-To"])
        self.assertIsNone(message["References"])

    def test_create_draft_returns_none_when_not_configured(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")

        self.assertIsNone(client.create_draft("t", "to@example.com", "s", "b", in_reply_to="<x@y>"))

    def test_send_draft_sends_the_draft_by_id(self):
        client, service = make_client_with_service()
        service.users().drafts().send().execute.return_value = {"id": "sent-1", "threadId": "t"}

        result = client.send_draft("draft-1")

        service.users().drafts().send.assert_called_with(userId="me", body={"id": "draft-1"})
        self.assertEqual(result, {"id": "sent-1", "threadId": "t"})

    def test_send_draft_returns_none_when_not_configured(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")

        self.assertIsNone(client.send_draft("draft-1"))


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


class ReceivedAtTests(unittest.TestCase):
    def test_parse_message_exposes_the_received_date_from_internal_date(self):
        message = {"id": "1", "threadId": "t", "internalDate": "1758833580000", "payload": {"headers": []}}

        self.assertEqual(GmailClient._parse_message(message).received_at, "2025-09-25")

    def test_missing_or_invalid_internal_date_gives_empty_received_at(self):
        for internal_date in ({}, {"internalDate": "abc"}):
            with self.subTest(internal_date=internal_date):
                message = {"id": "1", "threadId": "t", "payload": {"headers": []}, **internal_date}
                self.assertEqual(GmailClient._parse_message(message).received_at, "")


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


class MessageIdHeaderTests(unittest.TestCase):
    @staticmethod
    def parse_with_headers(headers):
        return GmailClient._parse_message({"id": "1", "threadId": "t", "payload": {"headers": headers}})

    def test_parse_message_exposes_the_message_id_header(self):
        for name in ("Message-ID", "Message-Id", "message-id"):
            with self.subTest(name=name):
                email = self.parse_with_headers([{"name": name, "value": "<abc@mail.example.com>"}])
                self.assertEqual(email.message_id_header, "<abc@mail.example.com>")

    def test_missing_message_id_header_gives_empty_string(self):
        self.assertEqual(self.parse_with_headers([]).message_id_header, "")

    def test_lowercase_from_and_subject_headers_are_read(self):
        email = self.parse_with_headers(
            [{"name": "from", "value": "Jane <jane@example.com>"}, {"name": "subject", "value": "Hi"}]
        )

        self.assertEqual((email.sender, email.subject), ("jane@example.com", "Hi"))


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


class UnreadQueryTests(unittest.TestCase):
    def test_default_query_limits_age_but_never_drops_a_category(self):
        self.assertEqual(build_unread_query(3), "is:unread in:inbox newer_than:3d")

    def test_fetch_unread_uses_configured_query(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="", unread_query="is:unread from:me")
        service = MagicMock()
        client._service = service
        service.users().messages().list().execute.return_value = {"messages": []}

        client.fetch_unread()

        self.assertEqual(service.users().messages().list.call_args.kwargs["q"], "is:unread from:me")
