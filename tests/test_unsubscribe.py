import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from prometheus_client import REGISTRY

from main import ApplicationContext
from src.config import Settings
from src.domain import CallbackEvent, EmailMessage, TriageResult
from src.gateways.unsubscribe_http import ONE_CLICK_BODY, HttpUnsubscriber
from src.gmail.client import GmailClient
from src.interactions import InteractionHandler
from src.interactions.callbacks import Callback, parse_callback, unsubscribe_buttons
from src.interactions.handlers import (
    ALREADY_UNSUBSCRIBED,
    KEPT,
    UNKNOWN_ACTION,
    UNSUBSCRIBE_FAILED,
    UNSUBSCRIBED,
)
from src.interactions.unsubscribe import (
    UnsubscribeProposer,
    offer_id,
    offer_key,
    offered_key,
    one_click_url,
)
from src.ports import UnsubscribeError
from src.storage import SqliteDecisionStore
from tests.fakes import FakeChat, fake_components

SENDER = "news@shop.example"
URL = "https://lists.shop.example/u?token=SECRET"
OFFER_MESSAGE_ID = 700


def email(message_id="m1", sender=SENDER, **fields) -> EmailMessage:
    defaults = {
        "dmarc_pass": True,
        "list_unsubscribe": f"<mailto:u@shop.example>, <{URL}>",
        "list_unsubscribe_post": "List-Unsubscribe=One-Click",
    }
    return EmailMessage(
        id=message_id, thread_id="t", sender=sender, subject="Promo", snippet="s", body="b",
        **{**defaults, **fields},
    )


def counted(status: str) -> float:
    return REGISTRY.get_sample_value("unsubscribes_total", {"status": status}) or 0.0


class OneClickUrlTests(unittest.TestCase):
    def test_picks_the_https_link_of_a_one_click_mail(self):
        self.assertEqual(one_click_url(email()), URL)
        self.assertEqual(one_click_url(email(list_unsubscribe_post="list-unsubscribe = one-click")), URL)

    def test_needs_the_one_click_header_an_https_link_and_an_authenticated_sender(self):
        untrusted = {
            "no post header": {"list_unsubscribe_post": ""},
            "mailto only": {"list_unsubscribe": "<mailto:u@shop.example>"},
            "plain http": {"list_unsubscribe": "<http://lists.shop.example/u>"},
            "no header": {"list_unsubscribe": ""},
            "unauthenticated": {"dmarc_pass": False},
        }
        for name, fields in untrusted.items():
            with self.subTest(name=name):
                self.assertIsNone(one_click_url(email(**fields)))

    def test_gmail_messages_carry_the_unsubscribe_headers(self):
        headers = [
            {"name": "From", "value": SENDER},
            {"name": "List-Unsubscribe", "value": f"<{URL}>"},
            {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
        ]

        message = GmailClient._parse_message({"id": "1", "payload": {"headers": headers}})

        self.assertEqual(
            (message.list_unsubscribe, message.list_unsubscribe_post),
            (f"<{URL}>", "List-Unsubscribe=One-Click"),
        )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, tzinfo=UTC)
        self.store = SqliteDecisionStore(":memory:", clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def decide(self, message_id, route="reject", sender=SENDER):
        triage = TriageResult("low", "promotion", 0.9, "jev")
        self.store.record_decision(email(message_id, sender=sender), triage, route)

    def archive(self, count, sender=SENDER):
        for index in range(count):
            self.decide(f"{sender}-{index}", sender=sender)


class ArchivedStreakTests(StoreTestCase):
    def test_counts_archived_mails_of_the_sender_whatever_the_case(self):
        self.archive(3)
        self.archive(2, sender="other@shop.example")

        self.assertEqual(self.store.archived_streak("News@Shop.example", timedelta(days=30)), 3)
        self.assertEqual(self.store.archived_streak("nobody@x.io", timedelta(days=30)), 0)

    def test_one_kept_mail_breaks_the_streak(self):
        self.archive(3)
        self.decide("kept", route="label")

        self.assertEqual(self.store.archived_streak(SENDER, timedelta(days=30)), 0)

    def test_a_mail_the_user_wanted_back_breaks_the_streak(self):
        for verdict in ("wrong_archive", "missed_urgent", "missed_important"):
            with self.subTest(verdict=verdict):
                self.archive(3)
                self.store.record_feedback(f"{SENDER}-0", verdict, origin="review")

                self.assertEqual(self.store.archived_streak(SENDER, timedelta(days=30)), 0)

    def test_only_the_window_counts(self):
        self.decide("old-kept", route="label")
        self.now += timedelta(days=31)
        self.archive(2)

        self.assertEqual(self.store.archived_streak(SENDER, timedelta(days=30)), 2)


class ProposerTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.chat = MagicMock()
        self.chat.send_message.return_value = OFFER_MESSAGE_ID
        self.proposer = UnsubscribeProposer(self.store, self.chat, min_archived=3)

    def test_no_offer_below_the_threshold(self):
        self.archive(2)

        self.assertFalse(self.proposer.consider(email()))
        self.chat.send_message.assert_not_called()

    def test_offers_once_with_buttons_and_keeps_the_link_server_side(self):
        self.archive(3)
        before = counted("offered")

        self.assertTrue(self.proposer.consider(email()))
        self.assertFalse(self.proposer.consider(email()))

        self.chat.send_message.assert_called_once()
        text = self.chat.send_message.call_args.args[0]
        buttons = self.chat.send_message.call_args.kwargs["buttons"]
        self.assertIn("3 mails archivés en 30 jours", text)
        self.assertIn("lists.shop.example", text)
        self.assertNotIn("SECRET", text + json.dumps(buttons))
        self.assertEqual(buttons, unsubscribe_buttons(offer_id(SENDER)))
        self.assertEqual(
            json.loads(self.store.get_state(offer_key(offer_id(SENDER)))),
            {"url": URL, "sender": SENDER, "offered_on": OFFER_MESSAGE_ID},
        )
        self.assertEqual(counted("offered"), before + 1)

    def test_no_offer_without_a_trustworthy_one_click_link(self):
        self.archive(3)

        self.assertFalse(self.proposer.consider(email(dmarc_pass=False)))
        self.assertIsNone(self.store.get_state(offered_key(SENDER)))

    def test_the_once_per_sender_marker_survives_pruning_but_the_link_does_not(self):
        self.archive(3)
        self.proposer.consider(email())
        self.now += timedelta(days=91)

        self.store.prune(timedelta(days=90))

        self.assertIsNotNone(self.store.get_state(offered_key(SENDER)))
        self.assertIsNone(self.store.get_state(offer_key(offer_id(SENDER))))

    def test_buttons_round_trip(self):
        row = unsubscribe_buttons("abc123")[0]

        self.assertEqual(
            [parse_callback(data) for _, data in row],
            [Callback("unsub", "abc123"), Callback("keep", "abc123")],
        )


class ConfirmationTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.chat = MagicMock()
        self.unsubscriber = MagicMock()
        self.handler = InteractionHandler(self.store, self.chat, MagicMock(), self.unsubscriber)
        self.offer = offer_id(SENDER)
        self.store.set_state(
            offer_key(self.offer),
            json.dumps({"url": URL, "sender": SENDER, "offered_on": OFFER_MESSAGE_ID}),
        )

    def press(self, action="unsub", offer=None, message_id=OFFER_MESSAGE_ID):
        data = f"{action}:{offer or self.offer}"
        self.handler.dispatch(CallbackEvent(callback_id="cb", message_id=message_id, data=data))
        return self.chat.answer_callback.call_args.args[1]

    def test_confirming_sends_the_stored_link_once(self):
        self.assertEqual(self.press(), UNSUBSCRIBED)
        self.assertEqual(self.press(), ALREADY_UNSUBSCRIBED)

        self.unsubscriber.unsubscribe.assert_called_once_with(URL)
        self.chat.clear_buttons.assert_called_with(OFFER_MESSAGE_ID)

    def test_request_is_claimed_before_it_is_sent(self):
        def check_claim(url):
            self.assertIsNotNone(self.store.get_state(f"unsub_done:{self.offer}"))

        self.unsubscriber.unsubscribe.side_effect = check_claim

        self.assertEqual(self.press(), UNSUBSCRIBED)

    def test_forged_offer_or_another_message_never_reaches_the_network(self):
        before = counted("rejected")

        with self.assertLogs("src.interactions.handlers", level="WARNING"):
            self.assertEqual(self.press(offer="deadbeefdeadbeef"), UNKNOWN_ACTION)
            self.assertEqual(self.press(message_id=999), UNKNOWN_ACTION)
            self.assertEqual(self.press(action="keep", message_id=999), UNKNOWN_ACTION)

        self.unsubscriber.unsubscribe.assert_not_called()
        self.assertEqual(counted("rejected"), before + 3)

    def test_failure_is_reported_without_the_link_and_not_retried(self):
        self.unsubscriber.unsubscribe.side_effect = UnsubscribeError("lists.shop.example answered HTTP 500")

        with self.assertLogs("src.interactions.handlers", level="WARNING") as logs:
            self.assertEqual(self.press(), UNSUBSCRIBE_FAILED)
        self.assertEqual(self.press(), ALREADY_UNSUBSCRIBED)

        self.unsubscriber.unsubscribe.assert_called_once()
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_keeping_sends_nothing_and_clears_the_buttons(self):
        self.assertEqual(self.press(action="keep"), KEPT)

        self.unsubscriber.unsubscribe.assert_not_called()
        self.chat.clear_buttons.assert_called_once_with(OFFER_MESSAGE_ID)

    def test_without_an_unsubscriber_the_request_fails_cleanly(self):
        handler = InteractionHandler(self.store, self.chat, MagicMock())

        with self.assertLogs("src.interactions.handlers", level="WARNING"):
            handler.dispatch(
                CallbackEvent(callback_id="cb", message_id=OFFER_MESSAGE_ID, data=f"unsub:{self.offer}")
            )

        self.assertEqual(self.chat.answer_callback.call_args.args[1], UNSUBSCRIBE_FAILED)


def resolves_to(*addresses):
    infos = [(None, None, None, "", (address, 443)) for address in addresses]
    return patch("src.gateways.unsubscribe_http.socket.getaddrinfo", return_value=infos)


class HttpUnsubscriberTests(unittest.TestCase):
    def setUp(self):
        self.unsubscriber = HttpUnsubscriber()

    def test_only_plain_https_links_are_accepted(self):
        for url in (
            "http://lists.shop.example/u",
            "https://user:pw@lists.shop.example/u",
            "https://lists.shop.example:8443/u",
            "https:///u",
            "https://[::1/u",
            "ftp://lists.shop.example/u",
        ):
            with self.subTest(url=url), self.assertRaises(UnsubscribeError):
                self.unsubscriber.unsubscribe(url)

    def test_non_public_addresses_are_refused_before_any_connection(self):
        cases = {
            "loopback": ["127.0.0.1"],
            "private": ["10.0.0.5"],
            "link-local metadata": ["169.254.169.254"],
            "ipv6 loopback": ["::1"],
            "one private among public": ["93.184.216.34", "192.168.1.10"],
            "no answer": [],
        }
        for name, addresses in cases.items():
            with (
                self.subTest(name=name),
                resolves_to(*addresses),
                patch.object(HttpUnsubscriber, "_post") as post,
                self.assertRaisesRegex(UnsubscribeError, "non-public"),
            ):
                self.unsubscriber.unsubscribe(URL)
            post.assert_not_called()

    def test_posts_the_one_click_body_to_the_checked_address(self):
        with (
            resolves_to("93.184.216.34"),
            patch("src.gateways.unsubscribe_http.socket.create_connection") as connect,
            patch("src.gateways.unsubscribe_http.ssl.create_default_context") as context,
            patch("src.gateways.unsubscribe_http.http.client.HTTPSConnection") as connection,
        ):
            connection.return_value.getresponse.return_value.status = 200

            self.unsubscriber.unsubscribe(URL)

        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        context.return_value.wrap_socket.assert_called_once_with(
            connect.return_value, server_hostname="lists.shop.example"
        )
        self.assertIs(connection.return_value.sock, context.return_value.wrap_socket.return_value)
        request = connection.return_value.request
        self.assertEqual(request.call_args.args, ("POST", "/u?token=SECRET"))
        self.assertEqual(request.call_args.kwargs["body"], ONE_CLICK_BODY)

    def test_redirects_and_errors_fail_without_leaking_the_link(self):
        for status in (302, 404, 500):
            with (
                self.subTest(status=status),
                resolves_to("93.184.216.34"),
                patch.object(HttpUnsubscriber, "_post", return_value=status),
                self.assertRaises(UnsubscribeError) as raised,
            ):
                self.unsubscriber.unsubscribe(URL)
            self.assertNotIn("SECRET", str(raised.exception))

    def test_network_failure_becomes_an_unsubscribe_error_without_the_link(self):
        with (
            resolves_to("93.184.216.34"),
            patch.object(HttpUnsubscriber, "_post", side_effect=OSError(f"cannot reach {URL}")),
            self.assertRaises(UnsubscribeError) as raised,
        ):
            self.unsubscriber.unsubscribe(URL)

        self.assertNotIn("SECRET", str(raised.exception))

    def test_unresolvable_host_is_refused(self):
        with (
            patch("src.gateways.unsubscribe_http.socket.getaddrinfo", side_effect=OSError("nxdomain")),
            self.assertRaisesRegex(UnsubscribeError, "does not resolve"),
        ):
            self.unsubscriber.unsubscribe(URL)


class WiringTests(unittest.TestCase):
    def make_ctx(self, chat=None, **settings):
        settings = Settings(_env_file=None, db_path=":memory:", **settings)
        ctx = ApplicationContext(settings, fake_components(chat=chat or FakeChat()))
        self.addCleanup(ctx.close)
        return ctx

    def test_off_by_default(self):
        self.assertIsNone(self.make_ctx().unsubscribes)

    def test_needs_the_inbound_listener_for_its_buttons(self):
        with self.assertLogs("gmail-assistant", level="WARNING"):
            ctx = self.make_ctx(unsubscribe_proposals_enabled=True)

        self.assertIsNone(ctx.unsubscribes)

    def test_enabled_with_inbound_it_considers_each_archived_mail(self):
        ctx = self.make_ctx(
            unsubscribe_proposals_enabled=True,
            telegram_inbound_enabled=True,
            unsubscribe_min_archived=2,
        )
        ctx.workflow = MagicMock()
        ctx.workflow.run.return_value = {
            "triage": TriageResult("low", "promotion", 0.9, "jev"),
            "route": "reject",
        }
        ctx.unsubscribes = MagicMock()
        ctx.unsubscribes.consider.side_effect = RuntimeError("chat down")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            ctx.process_email(email())  # a failed proposal must not fail the mail

        ctx.unsubscribes.consider.assert_called_once()
        self.assertEqual(ctx.mail.archived, ["m1"])


if __name__ == "__main__":
    unittest.main()
