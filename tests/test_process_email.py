import os
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from main import ALERTED_TTL_SECONDS, RETENTION, ApplicationContext
from src.bootstrap import build_components
from src.config import Settings
from src.domain import EmailMessage, TriageResult
from src.expiring_set import ExpiringSet
from tests.fakes import FakeChat, fake_components


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{"db_path": ":memory:", **overrides})


def make_context(route, result_extra=None, **settings):
    settings = make_settings(**settings)
    ctx = ApplicationContext(settings, replace(build_components(settings), mail=MagicMock()))
    ctx.alerts = MagicMock()
    ctx.alerts.send_urgent_alert.return_value = 555
    ctx.workflow = MagicMock()
    ctx.workflow.run.return_value = {
        "triage": TriageResult("high", "personnel", 0.9),
        "route": route,
        "summary": "sum",
        **(result_extra or {}),
    }
    return ctx


def make_email():
    return EmailMessage(
        id="m1",
        thread_id="t1",
        sender="a@b.com",
        subject="Hi",
        snippet="s",
        body="b",
        message_id_header="<abc@mail.example>",
    )


class UrgentHandlingTests(unittest.TestCase):
    def test_urgent_alerts_then_drafts_then_labels_in_that_order(self):
        ctx = make_context("llm", {"draft": "Bonjour"})
        calls = MagicMock()
        calls.attach_mock(ctx.alerts.send_urgent_alert, "alert")
        calls.attach_mock(ctx.mail.create_draft, "draft")
        calls.attach_mock(ctx.mail.label_message, "label")

        ctx.process_email(make_email())

        self.assertEqual([c[0] for c in calls.mock_calls], ["alert", "draft", "label"])

    def test_draft_replies_in_thread_with_the_original_message_id(self):
        ctx = make_context("llm", {"draft": "Bonjour"})

        ctx.process_email(make_email())

        ctx.mail.create_draft.assert_called_once_with(
            "t1", "a@b.com", "Hi", "Bonjour", in_reply_to="<abc@mail.example>"
        )

    def test_draft_failure_does_not_block_label(self):
        ctx = make_context("llm", {"draft": "Bonjour"})
        ctx.mail.create_draft.side_effect = RuntimeError("boom")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once_with("m1", "urgent")

    def test_no_draft_is_created_when_workflow_returned_none(self):
        ctx = make_context("llm")

        ctx.process_email(make_email())

        ctx.mail.create_draft.assert_not_called()

    def test_label_failure_retry_does_not_alert_or_rerun_workflow_again(self):
        ctx = make_context("llm", {"draft": "Bonjour"})
        ctx.mail.label_message.side_effect = [RuntimeError("boom"), None]
        email = make_email()

        with self.assertRaises(RuntimeError):
            ctx.process_email(email)
        ctx.process_email(email)

        ctx.alerts.send_urgent_alert.assert_called_once()
        ctx.workflow.run.assert_called_once()
        self.assertEqual(ctx.mail.label_message.call_count, 2)

    def test_alert_failure_leaves_mail_for_retry(self):
        ctx = make_context("llm")
        ctx.alerts.send_urgent_alert.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            ctx.process_email(make_email())

        ctx.mail.label_message.assert_not_called()
        self.assertFalse(ctx.store.was_alerted("m1"))

    def test_non_urgent_routes_never_alert(self):
        for route in ("label", "reject"):
            with self.subTest(route=route):
                ctx = make_context(route)
                ctx.process_email(make_email())
                ctx.alerts.send_urgent_alert.assert_not_called()


class DecisionPersistenceTests(unittest.TestCase):
    def test_every_processed_mail_is_recorded_with_its_route(self):
        for route in ("label", "reject", "llm"):
            with self.subTest(route=route):
                ctx = make_context(route)

                ctx.process_email(make_email())

                record = ctx.store.get("m1")
                self.assertEqual((record.route, record.urgency), (route, "high"))
                self.assertEqual(record.message_id_header, "<abc@mail.example>")

    def test_alert_is_linked_to_its_telegram_message(self):
        ctx = make_context("llm")

        ctx.process_email(make_email())

        self.assertEqual(ctx.store.find_by_chat_message(555).message_id, "m1")

    def test_alert_without_telegram_delivery_is_not_linked(self):
        ctx = make_context("llm")
        ctx.alerts.send_urgent_alert.return_value = None

        ctx.process_email(make_email())

        self.assertIsNone(ctx.store.get("m1").chat_message_id)

    def test_store_failure_after_alert_still_commits_the_label(self):
        ctx = make_context("llm")
        ctx.store = MagicMock()
        ctx.store.was_alerted.return_value = False
        ctx.store.mark_alerted.side_effect = RuntimeError("disk full")
        ctx.store.record_decision.side_effect = RuntimeError("disk full")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once_with("m1", "urgent")

    def test_failed_bookkeeping_and_label_still_alert_only_once(self):
        ctx = make_context("llm")
        ctx.store = MagicMock()
        ctx.store.was_alerted.return_value = False
        ctx.store.mark_alerted.side_effect = RuntimeError("disk full")
        ctx.mail.label_message.side_effect = RuntimeError("gmail down")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            with self.assertRaises(RuntimeError):
                ctx.process_email(make_email())
            with self.assertRaises(RuntimeError):
                ctx.process_email(make_email())

        ctx.alerts.send_urgent_alert.assert_called_once()
        ctx.workflow.run.assert_called_once()

    def test_alert_older_than_the_window_is_processed_again(self):
        ctx = make_context("llm")
        ctx.store.was_alerted = MagicMock(return_value=False)

        ctx.process_email(make_email())
        ctx.recently_alerted = ExpiringSet(ALERTED_TTL_SECONDS)
        ctx.process_email(make_email())

        self.assertEqual(ctx.alerts.send_urgent_alert.call_count, 2)
        ctx.store.was_alerted.assert_called_with("m1", within_seconds=ALERTED_TTL_SECONDS)

    def test_alerted_state_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "assistant.db")
            first = make_context("llm", db_path=db_path)
            first.mail.label_message.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                first.process_email(make_email())
            first.store.close()

            restarted = make_context("llm", db_path=db_path)
            restarted.process_email(make_email())
            restarted.store.close()

        restarted.alerts.send_urgent_alert.assert_not_called()
        restarted.workflow.run.assert_not_called()
        restarted.mail.label_message.assert_called_once_with("m1", "urgent")


class TelegramListenerTests(unittest.TestCase):
    def test_disabled_flag_starts_nothing(self):
        ctx = ApplicationContext(make_settings(telegram_bot_token="t", telegram_chat_id="1"))

        with patch("main.run_listener") as run:
            ctx.start_telegram_listener()

        self.assertIsNone(ctx.listener)
        run.assert_not_called()

    def test_enabled_without_bot_logs_a_warning(self):
        with self.assertLogs("gmail-assistant", level="WARNING"):
            ctx = ApplicationContext(make_settings(telegram_inbound_enabled=True))

        ctx.start_telegram_listener()

        self.assertIsNone(ctx.listener)
        self.assertFalse(ctx.alerts._feedback_buttons)

    def test_group_chat_without_allowlist_refuses_inbound(self):
        settings = make_settings(
            telegram_inbound_enabled=True, telegram_bot_token="t", telegram_chat_id="-100123"
        )
        with self.assertLogs("gmail-assistant", level="ERROR") as logs:
            ctx = ApplicationContext(settings)

        with patch("main.run_listener") as run:
            ctx.start_telegram_listener()

        run.assert_not_called()
        self.assertIsNone(ctx.listener)
        self.assertFalse(ctx.alerts._feedback_buttons)
        self.assertIn("TELEGRAM_ALLOWED_USER_IDS", "\n".join(logs.output))

    def test_group_chat_with_allowlist_enables_inbound(self):
        ctx = ApplicationContext(
            make_settings(
                telegram_inbound_enabled=True,
                telegram_bot_token="t",
                telegram_chat_id="-100123",
                telegram_allowed_user_ids="7, 8",
            )
        )

        self.assertTrue(ctx.inbound_enabled)
        self.assertTrue(ctx.alerts._feedback_buttons)
        self.assertTrue(ctx.chat.inbound_authorized)

    def test_enabled_runs_the_listener_with_the_shared_bot_and_handler(self):
        settings = make_settings(
            telegram_inbound_enabled=True, telegram_bot_token="t", telegram_chat_id="1"
        )
        ctx = ApplicationContext(settings)
        started = threading.Event()

        def fake_listener(bot, dispatch, load_offset, save_offset, stopping, sleep):
            self.assertIs(bot, ctx.chat)
            self.assertEqual(dispatch, ctx.interactions.dispatch)
            self.assertIs(stopping, ctx.stopping)
            save_offset(41)
            self.assertEqual(load_offset(), 41)
            started.set()

        with patch("main.run_listener", side_effect=fake_listener):
            ctx.start_telegram_listener()
            self.assertTrue(started.wait(2))
            ctx.close()

        self.assertTrue(ctx.listener.daemon)
        self.assertTrue(ctx.stopping.is_set())

    def test_offset_is_absent_on_first_start(self):
        self.assertIsNone(ApplicationContext(make_settings())._load_telegram_offset())

    def test_feedback_buttons_follow_the_inbound_flag(self):
        on = ApplicationContext(
            make_settings(
                telegram_inbound_enabled=True, telegram_bot_token="t", telegram_chat_id="1"
            )
        )
        off = ApplicationContext(make_settings(telegram_bot_token="t", telegram_chat_id="1"))

        self.assertTrue(on.alerts._feedback_buttons)
        self.assertFalse(off.alerts._feedback_buttons)


class ChatInboxSelectionTests(unittest.TestCase):
    def test_without_chat_inbox_inbound_is_refused_and_alerts_carry_no_buttons(self):
        settings = make_settings(telegram_inbound_enabled=True)
        with self.assertLogs("gmail-assistant", level="WARNING") as logs:
            ctx = ApplicationContext(settings, fake_components(chat=None))

        with patch("main.run_listener") as run:
            ctx.start_telegram_listener()

        run.assert_not_called()
        self.assertIsNone(ctx.interactions)
        self.assertFalse(ctx.alerts._feedback_buttons)
        self.assertIn("CHAT_INBOX", "\n".join(logs.output))

    def test_injected_chat_inbox_drives_inbound(self):
        chat = FakeChat()
        ctx = ApplicationContext(
            make_settings(telegram_inbound_enabled=True), fake_components(chat=chat)
        )

        self.assertTrue(ctx.inbound_enabled)
        self.assertIs(ctx.chat, chat)
        self.assertTrue(ctx.alerts._feedback_buttons)


class PruneTests(unittest.TestCase):
    def test_prunes_at_startup_then_at_most_once_a_day(self):
        ctx = ApplicationContext(make_settings())
        ctx.store = MagicMock()
        ctx.store.prune.return_value = 3

        with patch("main.monotonic", side_effect=[1000.0, 1000.0 + 3600, 1000.0 + 86400]):
            with self.assertLogs("gmail-assistant", level="INFO"):
                ctx.prune_if_due()
            ctx.prune_if_due()
            with self.assertLogs("gmail-assistant", level="INFO"):
                ctx.prune_if_due()

        self.assertEqual(ctx.store.prune.call_count, 2)
        ctx.store.prune.assert_called_with(RETENTION)

    def test_prune_failure_is_logged_and_not_retried_before_the_interval(self):
        ctx = ApplicationContext(make_settings())
        ctx.store = MagicMock()
        ctx.store.prune.side_effect = RuntimeError("locked")

        with patch("main.monotonic", side_effect=[0.0, 60.0]):
            with self.assertLogs("gmail-assistant", level="ERROR"):
                ctx.prune_if_due()
            ctx.prune_if_due()

        ctx.store.prune.assert_called_once()

    def test_retention_is_90_days(self):
        self.assertEqual(RETENTION.days, 90)

