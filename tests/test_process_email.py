import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, call, patch

from main import (
    ALERTED_TTL_SECONDS,
    PUT_FORWARD_LABEL,
    REPLY_EXPECTED_LABEL,
    RETENTION,
    ApplicationContext,
    poll_once,
)
from src.bootstrap import build_components
from src.config import Settings
from src.domain import EmailMessage, LLMAnalysis, TriageResult
from src.expiring_set import ExpiringSet
from src.observability.metrics import Metrics
from tests.fakes import FakeChat, FakeClassifier, FakeMail, fake_components


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

    def test_urgent_mail_expecting_a_reply_also_gets_the_reply_label(self):
        ctx = make_context("llm", {"draft": "Bonjour", "reply_expected": True})
        calls = MagicMock()
        calls.attach_mock(ctx.alerts.send_urgent_alert, "alert")
        calls.attach_mock(ctx.mail.create_draft, "draft")
        calls.attach_mock(ctx.mail.label_message, "label")

        ctx.process_email(make_email())

        self.assertEqual([c[0] for c in calls.mock_calls], ["alert", "draft", "label"])
        ctx.mail.label_message.assert_called_once_with("m1", REPLY_EXPECTED_LABEL, "urgent")

    def test_urgent_labeling_failure_leaves_the_mail_for_a_retry_that_only_relabels(self):
        ctx = make_context("llm", {"reply_expected": True, "draft": "Bonjour"})
        ctx.mail.label_message.side_effect = [RuntimeError("boom"), None]

        with self.assertRaises(RuntimeError):
            ctx.process_email(make_email())
        ctx.process_email(make_email())

        ctx.alerts.send_urgent_alert.assert_called_once()
        ctx.mail.create_draft.assert_called_once()
        self.assertEqual(ctx.mail.label_message.mock_calls[-1], call("m1", "urgent"))

    def test_urgent_mail_drafted_on_its_category_alone_keeps_a_single_label(self):
        ctx = make_context("llm", {"draft": "Bonjour"})

        ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once_with("m1", "urgent")

    def test_urgent_reply_outcome_is_counted(self):
        for extra, status in (({"draft": "Bonjour"}, "drafted"), ({}, "no_draft")):
            with self.subTest(status=status):
                counter = Metrics.reply_drafts.labels(status=status)
                before = counter._value.get()

                make_context("llm", {"reply_expected": True, **extra}).process_email(make_email())

                self.assertEqual(counter._value.get(), before + 1)

    def test_non_urgent_routes_never_alert(self):
        for route in ("label", "reject"):
            with self.subTest(route=route):
                ctx = make_context(route)
                ctx.process_email(make_email())
                ctx.alerts.send_urgent_alert.assert_not_called()


class ReplyExpectedTests(unittest.TestCase):
    def test_draft_then_both_labels_in_the_single_call_that_commits(self):
        ctx = make_context("label", {"reply_expected": True, "draft": "Bonjour"})
        calls = MagicMock()
        calls.attach_mock(ctx.mail.create_draft, "draft")
        calls.attach_mock(ctx.mail.label_message, "label")

        ctx.process_email(make_email())

        self.assertEqual(
            calls.mock_calls,
            [
                call.draft("t1", "a@b.com", "Hi", "Bonjour", in_reply_to="<abc@mail.example>"),
                call.label("m1", REPLY_EXPECTED_LABEL, "personnel"),
            ],
        )
        ctx.alerts.send_urgent_alert.assert_not_called()

    def test_mail_is_flagged_even_without_a_draft(self):
        ctx = make_context("label", {"reply_expected": True})

        ctx.process_email(make_email())

        ctx.mail.create_draft.assert_not_called()
        ctx.mail.label_message.assert_called_once_with("m1", REPLY_EXPECTED_LABEL, "personnel")

    def test_draft_failure_still_labels_the_mail(self):
        ctx = make_context("label", {"reply_expected": True, "draft": "Bonjour"})
        ctx.mail.create_draft.side_effect = RuntimeError("boom")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once_with("m1", REPLY_EXPECTED_LABEL, "personnel")

    def test_outcome_is_counted(self):
        for extra, status in (({"draft": "Bonjour"}, "drafted"), ({}, "no_draft")):
            with self.subTest(status=status):
                counter = Metrics.reply_drafts.labels(status=status)
                before = counter._value.get()

                make_context("label", {"reply_expected": True, **extra}).process_email(make_email())

                self.assertEqual(counter._value.get(), before + 1)

    def test_retry_after_a_failed_labeling_does_not_draft_twice(self):
        ctx = make_context("label", {"reply_expected": True, "draft": "Bonjour"})
        ctx.mail.label_message.side_effect = [RuntimeError("boom"), None]

        with self.assertRaises(RuntimeError):
            ctx.process_email(make_email())
        ctx.process_email(make_email())

        ctx.mail.create_draft.assert_called_once()
        self.assertEqual(
            ctx.mail.label_message.mock_calls[-1], call("m1", REPLY_EXPECTED_LABEL, "personnel")
        )

    def test_plain_label_route_is_unchanged(self):
        ctx = make_context("label")

        ctx.process_email(make_email())

        ctx.mail.create_draft.assert_not_called()
        ctx.mail.label_message.assert_called_once_with("m1", "personnel")

    def test_threshold_reaches_the_workflow_only_when_enabled(self):
        def threshold(**settings):
            settings = make_settings(**settings)
            return ApplicationContext(settings, build_components(settings)).workflow.needs_reply_threshold

        self.assertIsNone(threshold())
        self.assertIsNone(threshold(needs_reply_threshold=0.7))
        self.assertEqual(threshold(needs_reply_enabled=True), 0.5)
        self.assertEqual(threshold(needs_reply_enabled=True, needs_reply_threshold=0.7), 0.7)


class PutForwardTests(unittest.TestCase):
    def test_label_worth_seeing_goes_with_the_category_label_in_one_call(self):
        ctx = make_context("label", {"attention": ("service_change",)})

        ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once_with("m1", PUT_FORWARD_LABEL, "personnel")
        ctx.alerts.send_urgent_alert.assert_not_called()
        ctx.mail.archive_message.assert_not_called()
        self.assertTrue(ctx.store.get("m1").put_forward)

    def test_a_failed_labeling_leaves_the_mail_unhandled_for_the_next_cycle(self):
        ctx = make_context("label", {"attention": ("service_change",)})
        ctx.mail.label_message.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once()

    def test_reply_label_worth_seeing_label_and_category_label_in_one_call(self):
        ctx = make_context(
            "label", {"attention": ("needs_reply",), "reply_expected": True, "draft": "Bonjour"}
        )

        ctx.process_email(make_email())

        ctx.mail.label_message.assert_called_once_with(
            "m1", REPLY_EXPECTED_LABEL, PUT_FORWARD_LABEL, "personnel"
        )

    def test_put_forward_mails_are_counted(self):
        before = Metrics.put_forward._value.get()

        make_context("label", {"attention": ("personal_event",)}).process_email(make_email())
        make_context("label").process_email(make_email())

        self.assertEqual(Metrics.put_forward._value.get(), before + 1)

    def test_a_plain_decision_is_stored_as_not_put_forward(self):
        ctx = make_context("label")

        ctx.process_email(make_email())

        self.assertFalse(ctx.store.get("m1").put_forward)

    def test_threshold_reaches_the_workflow_only_when_the_mode_is_on(self):
        def threshold(**settings):
            settings = make_settings(**settings)
            return ApplicationContext(settings, build_components(settings)).workflow.attention_threshold

        self.assertIsNone(threshold())
        self.assertIsNone(threshold(attention_mode="shadow"))
        self.assertEqual(threshold(attention_mode="on"), 0.5)
        self.assertEqual(threshold(attention_mode="on", attention_threshold=0.7), 0.7)


class DailyListTests(unittest.TestCase):
    def context(self, chat=None, **settings):
        settings = make_settings(**settings)
        components = fake_components(chat=chat or FakeChat())
        self.addCleanup(components.store.close)
        ctx = ApplicationContext(settings, components)
        if ctx.interactions.put_forward is not None:
            ctx.interactions.put_forward = MagicMock()
            ctx.interactions.put_forward.send_daily.return_value = 2
        return ctx

    def test_job_needs_the_mode_on_an_hour_and_a_chat(self):
        on = {"attention_mode": "on", "attention_list_hour": 0}

        self.assertIsNotNone(self.context(**on).put_forward_job)
        self.assertIsNone(self.context().put_forward_job)
        self.assertIsNone(self.context(attention_mode="on").put_forward_job)
        self.assertIsNone(self.context(attention_mode="shadow", attention_list_hour=0).put_forward_job)
        with self.assertLogs("gmail-assistant", level="WARNING"):
            self.assertIsNone(self.context(chat=FakeChat(is_configured=False), **on).put_forward_job)

    def test_list_is_sent_once_a_day_with_buttons_only_when_inbound_is_on(self):
        ctx = self.context(attention_mode="on", attention_list_hour=0)

        ctx.send_put_forward_list_if_due()
        ctx.send_put_forward_list_if_due()

        ctx.interactions.put_forward.send_daily.assert_called_once_with(interactive=False)

    def test_nothing_is_sent_without_a_job(self):
        ctx = self.context(attention_mode="on")

        ctx.send_put_forward_list_if_due()

        ctx.interactions.put_forward.send_daily.assert_not_called()

    def test_list_and_command_only_exist_when_the_mode_is_on(self):
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                settings = make_settings(attention_mode=mode)
                components = fake_components()
                self.addCleanup(components.store.close)

                self.assertIsNone(ApplicationContext(settings, components).interactions.put_forward)

    def test_a_store_failure_when_claiming_the_day_does_not_stop_the_poll(self):
        ctx = self.context(attention_mode="on", attention_list_hour=0)
        ctx.put_forward_job = MagicMock()
        ctx.put_forward_job.claim.side_effect = sqlite3.OperationalError("database is locked")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            ctx.send_put_forward_list_if_due()

        ctx.interactions.put_forward.send_daily.assert_not_called()

    def test_a_list_that_went_out_uses_one_unit_of_the_daily_budget(self):
        ctx = self.context(attention_mode="on", attention_list_hour=0, proactive_daily_cap=2)

        ctx.send_put_forward_list_if_due()

        self.assertEqual(ctx.proactive_budget.available(), 1)

    def test_an_empty_list_uses_no_budget(self):
        ctx = self.context(attention_mode="on", attention_list_hour=0, proactive_daily_cap=2)
        ctx.interactions.put_forward.send_daily.return_value = 0

        ctx.send_put_forward_list_if_due()

        self.assertEqual(ctx.proactive_budget.available(), 2)

    def test_a_spent_budget_holds_the_list_without_claiming_the_day(self):
        ctx = self.context(attention_mode="on", attention_list_hour=0, proactive_daily_cap=1)
        ctx.proactive_budget.spend()

        ctx.send_put_forward_list_if_due()

        ctx.interactions.put_forward.send_daily.assert_not_called()
        self.assertIsNone(ctx.store.get_state("job:put_forward_list"))

    def test_a_failed_list_is_logged_and_does_not_stop_the_poll(self):
        ctx = self.context(attention_mode="on", attention_list_hour=0)
        ctx.interactions.put_forward.send_daily.side_effect = RuntimeError("telegram down")
        ctx.mail = MagicMock()
        ctx.mail.fetch_unread.return_value = []

        with self.assertLogs("gmail-assistant", level="ERROR"):
            poll_once(ctx)

        ctx.mail.fetch_unread.assert_called_once()


class FollowUpWiringTests(unittest.TestCase):
    def context(self, mail=None, **settings):
        settings = make_settings(**settings)
        components = fake_components()
        if mail is not None:
            components = replace(components, mail=mail)
        self.addCleanup(components.store.close)
        return ApplicationContext(settings, components)

    def test_tracking_needs_a_mode_and_a_configured_mailbox(self):
        self.assertIsNone(self.context().follow_ups)
        self.assertIsNotNone(self.context(follow_up_mode="shadow").follow_ups)
        self.assertIsNotNone(self.context(follow_up_mode="on").follow_ups)
        with self.assertLogs("gmail-assistant", level="WARNING"):
            ctx = self.context(mail=FakeMail(is_configured=False), follow_up_mode="shadow")
        self.assertIsNone(ctx.follow_ups)

    def test_refresh_runs_at_most_once_per_interval(self):
        ctx = self.context(follow_up_mode="shadow")
        ctx.follow_ups = MagicMock()

        ctx.refresh_follow_ups_if_due()
        ctx.refresh_follow_ups_if_due()

        ctx.follow_ups.refresh.assert_called_once_with()

    def test_a_failed_refresh_is_logged_and_does_not_stop_the_poll(self):
        ctx = self.context(follow_up_mode="shadow")
        ctx.follow_ups = MagicMock()
        ctx.follow_ups.refresh.side_effect = RuntimeError("boom")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            ctx.refresh_follow_ups_if_due()

    def test_nothing_runs_when_tracking_is_off(self):
        ctx = self.context()

        ctx.refresh_follow_ups_if_due()


class HealthWiringTests(unittest.TestCase):
    def context(self, chat=None, **settings):
        settings = make_settings(**settings)
        components = fake_components(chat=chat or FakeChat())
        self.addCleanup(components.store.close)
        return ApplicationContext(settings, components)

    def check_names(self, **settings):
        watch = self.context(**settings).health_watch
        return None if watch is None else [check.name for check in watch._checks]

    def test_failure_counters_are_watched_by_default(self):
        self.assertEqual(self.check_names(), ["jev_fallback", "emails_skipped", "llm_errors"])

    def test_a_zero_window_turns_the_watch_off(self):
        self.assertIsNone(self.check_names(health_alert_window_minutes=0))

    def test_listener_and_budget_are_watched_only_when_they_exist(self):
        names = self.check_names(telegram_inbound_enabled=True, llm_daily_budget_usd=0.5)

        self.assertEqual(names[-2:], ["chat_listener", "llm_budget"])

    def test_problems_are_sent_to_the_chat(self):
        ctx = self.context()
        ctx.alerts = MagicMock()
        ctx.health_watch = ApplicationContext._build_health_watch(ctx)
        ctx.health_watch._checks[0].problem()
        for _ in range(3):
            Metrics.mark_jev_fallback()

        ctx.check_health()

        self.assertIn("JEV ne répond pas", ctx.alerts.send_text.call_args.args[0])

    def test_over_budget_the_analyzer_is_not_called_and_the_alert_still_goes_out(self):
        analyzer = MagicMock()
        settings = make_settings(llm_daily_budget_usd=0.5)
        components = fake_components(
            analyzer=analyzer, classifier=FakeClassifier(TriageResult("high", "personnel", 0.9))
        )
        self.addCleanup(components.store.close)
        ctx = ApplicationContext(settings, components)
        ctx.alerts = MagicMock()
        ctx.alerts.send_urgent_alert.return_value = None
        Metrics.llm_cost_usd.labels(kind="analysis").inc(0.6)

        with self.assertLogs("src.health.spend", level="WARNING"):
            ctx.process_email(make_email())

        analyzer.analyze.assert_not_called()
        summary = ctx.alerts.send_urgent_alert.call_args.args[2]
        self.assertIn("(résumé indisponible)", summary)

    def test_without_a_budget_the_analyzer_is_called_whatever_was_spent(self):
        analyzer = MagicMock()
        analyzer.analyze.return_value = LLMAnalysis(summary="sum")
        components = fake_components(
            analyzer=analyzer, classifier=FakeClassifier(TriageResult("high", "personnel", 0.9))
        )
        self.addCleanup(components.store.close)
        ctx = ApplicationContext(make_settings(), components)
        ctx.alerts = MagicMock()
        ctx.alerts.send_urgent_alert.return_value = None
        Metrics.llm_cost_usd.labels(kind="analysis").inc(50)

        ctx.process_email(make_email())

        analyzer.analyze.assert_called_once()


class AttentionShadowTests(unittest.TestCase):
    def process(self, **settings):
        ctx = make_context("reject", **settings)
        ctx.workflow.run.return_value["triage"] = TriageResult(
            "low", "notification_systeme", 0.9, "jev", 0.1, {"service_change": 0.99}
        )
        counter = Metrics.attention_signals.labels(signal="service_change")
        before = counter._value.get()

        ctx.process_email(make_email())

        return ctx, counter._value.get() - before

    def test_shadow_mode_counts_the_reason_and_changes_nothing_else(self):
        ctx, counted = self.process(attention_mode="shadow")

        self.assertEqual(counted, 1)
        ctx.mail.archive_message.assert_called_once_with("m1")
        ctx.mail.label_message.assert_not_called()
        ctx.alerts.send_urgent_alert.assert_not_called()

    def test_nothing_is_counted_when_the_mode_is_off(self):
        _, counted = self.process()

        self.assertEqual(counted, 0)


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


class BackupTests(unittest.TestCase):
    def make_ctx(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = ApplicationContext(make_settings(backup_dir=tmp.name))
        ctx.backups = MagicMock()
        return ctx

    def test_disabled_without_a_backup_directory(self):
        ctx = ApplicationContext(make_settings())

        self.assertIsNone(ctx.backups)
        ctx.backup_if_due()  # must not raise

    def test_configured_directory_and_retention_reach_the_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = ApplicationContext(make_settings(backup_dir=directory, backup_keep=3))
            with self.assertLogs("gmail-assistant", level="INFO"):
                ctx.backup_if_due()

            self.assertEqual(len(os.listdir(directory)), 1)
            self.assertEqual(ctx.backups._keep, 3)

    def test_backs_up_at_startup_then_at_most_once_a_day(self):
        ctx = self.make_ctx()

        with patch("main.monotonic", side_effect=[1000.0, 1000.0 + 3600, 1000.0 + 86400]):
            with self.assertLogs("gmail-assistant", level="INFO"):
                ctx.backup_if_due()
            ctx.backup_if_due()
            with self.assertLogs("gmail-assistant", level="INFO"):
                ctx.backup_if_due()

        self.assertEqual(ctx.backups.run.call_count, 2)

    def test_failure_is_logged_counted_and_not_retried_before_the_interval(self):
        ctx = self.make_ctx()
        ctx.backups.run.side_effect = RuntimeError("disk full")
        before = Metrics.backup_failures._value.get()

        with patch("main.monotonic", side_effect=[0.0, 60.0]):
            with self.assertLogs("gmail-assistant", level="ERROR"):
                ctx.backup_if_due()
            ctx.backup_if_due()

        ctx.backups.run.assert_called_once()
        self.assertEqual(Metrics.backup_failures._value.get(), before + 1)

    def test_backup_and_prune_keep_separate_schedules(self):
        ctx = self.make_ctx()
        ctx.store = MagicMock()
        ctx.store.prune.return_value = 0

        with patch("main.monotonic", return_value=5.0):
            ctx.backup_if_due()
            ctx.prune_if_due()

        ctx.backups.run.assert_called_once()
        ctx.store.prune.assert_called_once()

