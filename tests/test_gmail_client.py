import base64
import unittest
from datetime import UTC, datetime
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

    def test_label_message_adds_every_label_and_marks_read_in_a_single_change(self):
        client, service = make_client_with_service()
        service.users().labels().list().execute.return_value = {
            "labels": [
                {"name": "Assistant/A_voir", "id": "label-see"},
                {"name": "Assistant/Personnel", "id": "label-cat"},
            ]
        }
        service.users().messages().modify.reset_mock()

        client.label_message("msg-1", "a_voir", "personnel")

        service.users().messages().modify.assert_called_once_with(
            userId="me",
            id="msg-1",
            body={"addLabelIds": ["label-see", "label-cat"], "removeLabelIds": ["UNREAD"]},
        )

    def test_a_label_that_cannot_be_created_leaves_the_mail_untouched(self):
        client, service = make_client_with_service()
        service.users().labels().list().execute.return_value = {
            "labels": [{"name": "Assistant/Personnel", "id": "label-cat"}]
        }
        service.users().labels().create().execute.side_effect = RuntimeError("quota")
        service.users().messages().modify.reset_mock()

        with self.assertRaises(RuntimeError):
            client.label_message("msg-1", "a_voir", "personnel")

        service.users().messages().modify.assert_not_called()

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

    def test_create_draft_for_an_existing_caller_is_unchanged(self):
        message = self.create_and_parse_draft(in_reply_to="<abc@mail.example.com>")

        self.assertIsNone(message["Cc"])
        self.assertEqual(
            sorted(message.keys()),
            sorted(["Content-Type", "MIME-Version", "Content-Transfer-Encoding", "To", "Subject",
                    "In-Reply-To", "References"]),
        )

    def test_create_draft_copies_and_references_the_whole_thread(self):
        message = self.create_and_parse_draft(
            in_reply_to="<b@x>", cc=["c@example.com", "d@example.com"], references=["<a@x>", "<b@x>"]
        )

        self.assertEqual(message["Cc"], "c@example.com, d@example.com")
        self.assertEqual(message["In-Reply-To"], "<b@x>")
        self.assertEqual(message["References"], "<a@x> <b@x>")

    def test_create_draft_returns_the_draft_id(self):
        client, service = make_client_with_service()
        service.users().drafts().create().execute.return_value = {"id": "draft-1", "message": {}}

        self.assertEqual(client.create_draft("t", "to@example.com", "s", "b"), "draft-1")

    def test_create_draft_returns_none_when_not_configured(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")

        self.assertIsNone(client.create_draft("t", "to@example.com", "s", "b", in_reply_to="<x@y>"))

    def test_send_draft_sends_the_draft_by_id(self):
        client, service = make_client_with_service()
        service.users().drafts().send().execute.return_value = {"id": "sent-1", "threadId": "t"}

        result = client.send_draft("draft-1")

        service.users().drafts().send.assert_called_with(userId="me", body={"id": "draft-1"})
        self.assertTrue(result)

    def test_send_draft_returns_false_when_not_configured(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")

        self.assertFalse(client.send_draft("draft-1"))


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

    def test_fetch_unread_raises_once_429_retries_are_exhausted(self):
        client, service = make_client_with_service()
        service.users().messages().list().execute.side_effect = HttpError(
            resp=FakeResponse(429), content=b"rate limited"
        )

        with patch("time.sleep") as sleep, self.assertRaises(HttpError):
            client.fetch_unread(max_retries=3)

        self.assertEqual(service.users().messages().list().execute.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_fetch_unread_reraises_non_429_errors(self):
        client, service = make_client_with_service()
        service.users().messages().list().execute.side_effect = HttpError(
            resp=FakeResponse(500), content=b"server error"
        )

        with self.assertRaises(HttpError):
            client.fetch_unread(max_retries=3)


def header(name, value):
    return {"name": name, "value": value}


def thread_message(id_, sender, labels=(), internal_ms="1759658400000", **extra_headers):
    headers = [header("From", sender), header("To", "Jean <jean@example.com>, b@example.com")]
    headers += [header(name.replace("_", "-"), value) for name, value in extra_headers.items()]
    return {"id": id_, "labelIds": list(labels), "internalDate": internal_ms, "payload": {"headers": headers}}


class SentMailTests(unittest.TestCase):
    def client(self, aliases=("me@example.com", "Alias@Example.com")):
        client, service = make_client_with_service()
        service.users().settings().sendAs().list().execute.return_value = {
            "sendAs": [{"sendAsEmail": address} for address in aliases]
        }
        return client, service

    def test_my_addresses_are_every_alias_lowercased_and_read_once(self):
        client, service = self.client()

        self.assertEqual(client.my_addresses(), frozenset({"me@example.com", "alias@example.com"}))
        client.my_addresses()

        self.assertEqual(service.users().settings().sendAs().list().execute.call_count, 1)

    def test_sent_threads_lists_recent_threads_with_their_history_id(self):
        client, service = self.client()
        service.users().threads().list().execute.return_value = {
            "threads": [{"id": "t1", "historyId": 42}, {"id": "t2", "historyId": "7"}]
        }

        refs = client.sent_threads(newer_than_days=14, limit=50)

        self.assertEqual([(r.thread_id, r.history_id) for r in refs], [("t1", "42"), ("t2", "7")])
        kwargs = service.users().threads().list.call_args.kwargs
        self.assertEqual((kwargs["q"], kwargs["maxResults"]), ("in:sent newer_than:14d", 50))

    def test_thread_snapshot_reads_headers_only(self):
        client, service = self.client()
        service.users().threads().get().execute.return_value = {
            "id": "t1",
            "historyId": "99",
            "messages": [
                thread_message("m1", "Me <me@example.com>", labels=["SENT"], Message_ID="<m1@x>",
                               Cc="c@example.com", Subject="Devis"),
                thread_message("m2", "Jean <Jean@Example.com>"),
            ],
        }

        snapshot = client.thread_snapshot("t1")

        self.assertEqual(service.users().threads().get.call_args.kwargs["format"], "metadata")
        self.assertEqual(snapshot.history_id, "99")
        mine, theirs = snapshot.messages
        self.assertTrue(mine.from_me)
        self.assertEqual(mine.to, ("jean@example.com", "b@example.com"))
        self.assertEqual((mine.cc, mine.subject, mine.message_id_header), (("c@example.com",), "Devis", "<m1@x>"))
        self.assertEqual(mine.sent_at, datetime(2025, 10, 5, 10, 0, tzinfo=UTC))
        self.assertEqual(theirs.sender, "jean@example.com")
        self.assertFalse(theirs.from_me or theirs.automated or theirs.bounce)

    def test_drafts_are_left_out_of_the_thread(self):
        client, service = self.client()
        service.users().threads().get().execute.return_value = {
            "messages": [
                thread_message("m1", "jean@example.com"),
                thread_message("d1", "me@example.com", labels=["DRAFT"]),
            ]
        }

        self.assertEqual([m.id for m in client.thread_snapshot("t1").messages], ["m1"])

    def test_a_message_from_an_alias_is_mine_even_without_the_sent_label(self):
        client, service = self.client()
        service.users().threads().get().execute.return_value = {
            "messages": [thread_message("m1", "alias@example.com")]
        }

        self.assertTrue(client.thread_snapshot("t1").messages[0].from_me)

    def test_automated_messages_are_recognised_by_their_headers(self):
        cases = {
            "auto-submitted": {"Auto_Submitted": "auto-replied"},
            "precedence": {"Precedence": "auto_reply"},
            "x-autoreply": {"X_Autoreply": "yes"},
            "x-autorespond": {"X_Autorespond": "yes"},
        }
        for name, headers in cases.items():
            with self.subTest(name):
                client, service = self.client()
                service.users().threads().get().execute.return_value = {
                    "messages": [thread_message("m1", "jean@example.com", **headers)]
                }
                self.assertTrue(client.thread_snapshot("t1").messages[0].automated)

    def test_a_person_answering_through_a_mailing_list_is_not_automated(self):
        for headers in ({"List_Id": "<team.example.com>"}, {"Precedence": "list"}, {"Precedence": "bulk"}):
            with self.subTest(headers):
                client, service = self.client()
                service.users().threads().get().execute.return_value = {
                    "messages": [thread_message("m1", "jean@example.com", **headers)]
                }
                self.assertFalse(client.thread_snapshot("t1").messages[0].automated)

    def test_my_plus_tag_and_dotted_gmail_spellings_are_mine(self):
        client, service = self.client(aliases=("first.last@gmail.com",))
        service.users().threads().get().execute.return_value = {
            "messages": [thread_message("m1", "FirstLast+news@googlemail.com")]
        }

        self.assertTrue(client.thread_snapshot("t1").messages[0].from_me)

    def test_auto_submitted_no_is_a_person(self):
        client, service = self.client()
        service.users().threads().get().execute.return_value = {
            "messages": [thread_message("m1", "jean@example.com", Auto_Submitted="no")]
        }

        self.assertFalse(client.thread_snapshot("t1").messages[0].automated)

    def test_a_delivery_failure_is_a_bounce(self):
        client, service = self.client()
        service.users().threads().get().execute.return_value = {
            "messages": [thread_message("m1", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>")]
        }

        message = client.thread_snapshot("t1").messages[0]

        self.assertTrue(message.bounce and message.automated)

    def test_other_errors_reading_a_thread_are_raised(self):
        client, service = self.client()
        service.users().threads().get().execute.side_effect = HttpError(FakeResponse(500), b"boom")

        with self.assertRaises(HttpError):
            client.thread_snapshot("t1")

    def test_a_message_without_a_date_is_dated_at_the_epoch(self):
        client, service = self.client()
        service.users().threads().get().execute.return_value = {
            "messages": [thread_message("m1", "jean@example.com", internal_ms="soon")]
        }

        self.assertEqual(client.thread_snapshot("t1").messages[0].sent_at.year, 1970)

    def test_a_deleted_thread_gives_none(self):
        client, service = self.client()
        service.users().threads().get().execute.side_effect = HttpError(FakeResponse(404), b"gone")

        self.assertIsNone(client.thread_snapshot("t1"))

    def test_sent_text_keeps_only_what_i_wrote(self):
        client, service = self.client()
        body = "Peux-tu me renvoyer le devis ?\n-- \nMe\n\nLe 5 oct., Jean a écrit :\n> Voici"
        service.users().messages().get().execute.return_value = {
            "payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}}
        }

        self.assertEqual(client.sent_text("m1"), "Peux-tu me renvoyer le devis ?")

    def test_has_message_from_searches_every_folder_after_the_date(self):
        client, service = self.client()
        service.users().messages().list().execute.return_value = {"messages": [{"id": "x"}]}

        found = client.has_message_from("jean@example.com", datetime(2026, 10, 5, tzinfo=UTC))

        self.assertTrue(found)
        query = service.users().messages().list.call_args.kwargs["q"]
        self.assertEqual(query, "from:jean@example.com after:1791158400 in:anywhere")

    def test_has_message_from_is_false_when_nothing_came(self):
        client, service = self.client()
        service.users().messages().list().execute.return_value = {}

        self.assertFalse(client.has_message_from("jean@example.com", datetime(2026, 10, 5, tzinfo=UTC)))

    def test_an_address_that_could_alter_the_query_is_never_searched(self):
        client, service = self.client()
        service.users().messages().list.reset_mock()

        for address in ("jean@example.com OR in:sent", "", "jean"):
            with self.subTest(address=address):
                self.assertTrue(client.has_message_from(address, datetime(2026, 10, 5, tzinfo=UTC)))
        service.users().messages().list.assert_not_called()

    def test_unconfigured_client_reads_nothing_and_never_says_unanswered(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")

        self.assertEqual(client.my_addresses(), frozenset())
        self.assertEqual(client.sent_threads(14, 50), [])
        self.assertIsNone(client.thread_snapshot("t1"))
        self.assertEqual(client.sent_text("m1"), "")
        self.assertTrue(client.has_message_from("jean@example.com", datetime(2026, 10, 5, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()


class SearchTests(unittest.TestCase):
    def test_search_runs_the_query_with_its_limit_and_parses_what_it_finds(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")
        service = MagicMock()
        client._service = service
        service.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
        service.users().messages().get().execute.return_value = {
            "id": "m1",
            "threadId": "t1",
            "snippet": "Voici le devis",
            "payload": {"headers": [{"name": "From", "value": "Jean <jean@example.com>"}]},
        }

        found, = client.search("from:jean devis", 3)

        listed = service.users().messages().list.call_args.kwargs
        self.assertEqual(listed["q"], "(from:jean devis) -in:draft -in:spam -in:trash")
        self.assertEqual(listed["maxResults"], 3)
        self.assertEqual((found.id, found.thread_id, found.sender), ("m1", "t1", "jean@example.com"))

    def test_search_finds_nothing_when_gmail_is_not_configured(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")

        self.assertEqual(client.search("devis", 3), [])


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
