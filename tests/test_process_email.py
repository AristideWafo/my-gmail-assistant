import unittest
from unittest.mock import MagicMock

from main import ApplicationContext
from src.config import Settings
from src.gmail.client import EmailMessage
from src.triage.engine import TriageResult


def make_context(route, result_extra=None):
    ctx = ApplicationContext(Settings(_env_file=None))
    ctx.gmail = MagicMock()
    ctx.alerts = MagicMock()
    ctx.workflow = MagicMock()
    ctx.workflow.run.return_value = {
        "triage": TriageResult("high", "personnel", 0.9),
        "route": route,
        "summary": "sum",
        **(result_extra or {}),
    }
    return ctx


def make_email():
    return EmailMessage(id="m1", thread_id="t1", sender="a@b.com", subject="Hi", snippet="s", body="b")


class UrgentHandlingTests(unittest.TestCase):
    def test_urgent_alerts_then_drafts_then_labels_in_that_order(self):
        ctx = make_context("llm", {"draft": "Bonjour"})
        calls = MagicMock()
        calls.attach_mock(ctx.alerts.send_urgent_alert, "alert")
        calls.attach_mock(ctx.gmail.create_draft, "draft")
        calls.attach_mock(ctx.gmail.label_message, "label")

        ctx.process_email(make_email())

        self.assertEqual([c[0] for c in calls.mock_calls], ["alert", "draft", "label"])

    def test_draft_failure_does_not_block_label(self):
        ctx = make_context("llm", {"draft": "Bonjour"})
        ctx.gmail.create_draft.side_effect = RuntimeError("boom")

        ctx.process_email(make_email())

        ctx.gmail.label_message.assert_called_once_with("m1", "urgent")

    def test_no_draft_is_created_when_workflow_returned_none(self):
        ctx = make_context("llm")

        ctx.process_email(make_email())

        ctx.gmail.create_draft.assert_not_called()

    def test_label_failure_retry_does_not_alert_or_rerun_workflow_again(self):
        ctx = make_context("llm", {"draft": "Bonjour"})
        ctx.gmail.label_message.side_effect = [RuntimeError("boom"), None]
        email = make_email()

        with self.assertRaises(RuntimeError):
            ctx.process_email(email)
        ctx.process_email(email)

        ctx.alerts.send_urgent_alert.assert_called_once()
        ctx.workflow.run.assert_called_once()
        self.assertEqual(ctx.gmail.label_message.call_count, 2)

    def test_alert_failure_leaves_mail_for_retry(self):
        ctx = make_context("llm")
        ctx.alerts.send_urgent_alert.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            ctx.process_email(make_email())

        ctx.gmail.label_message.assert_not_called()

    def test_non_urgent_routes_never_alert(self):
        for route in ("label", "reject"):
            with self.subTest(route=route):
                ctx = make_context(route)
                ctx.process_email(make_email())
                ctx.alerts.send_urgent_alert.assert_not_called()
