import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from main import create_app, poll_once, sync_history_once
from src.health import PollHealth, run_watchdog
from src.observability.metrics import Metrics


def make_ctx():
    ctx = MagicMock()
    ctx.stopping.is_set.return_value = False
    return ctx


class PollHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.health = PollHealth(stale_after_seconds=100, clock=lambda: self.now)

    def test_healthy_right_after_start_and_after_a_beat(self):
        self.assertIsNone(self.health.problem(task_done=False))
        self.now = 90
        self.health.beat()
        self.now = 150
        self.assertIsNone(self.health.problem(task_done=False))

    def test_stalled_loop_is_reported_with_its_idle_time(self):
        self.now = 101
        self.assertIn("no polling activity for 101s", self.health.problem(task_done=False))

    def test_finished_task_is_reported_regardless_of_heartbeat(self):
        self.assertEqual(self.health.problem(task_done=True), "polling task has stopped")


class WatchdogTests(unittest.TestCase):
    def test_calls_on_failure_once_with_the_reason_then_stops(self):
        health = PollHealth(stale_after_seconds=100)
        failures = []

        asyncio.run(run_watchdog(health, lambda: True, failures.append, interval_seconds=0))

        self.assertEqual(failures, ["polling task has stopped"])

    def test_keeps_watching_while_healthy(self):
        health = PollHealth(stale_after_seconds=100)
        checks = []

        def task_done():
            checks.append(1)
            return len(checks) >= 3

        failures = []
        asyncio.run(run_watchdog(health, task_done, failures.append, interval_seconds=0))

        self.assertEqual(len(checks), 3)
        self.assertEqual(len(failures), 1)


class HealthzEndpointTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"DB_PATH": ":memory:"})
        env.start()
        self.addCleanup(env.stop)

    def call_healthz(self, app):
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/healthz")
        return asyncio.run(endpoint())

    def test_ok_while_polling_is_alive(self):
        self.assertEqual(self.call_healthz(create_app()), {"status": "ok"})

    def test_503_when_the_polling_task_is_dead(self):
        app = create_app()

        async def finished():
            return None

        async def scenario():
            task = asyncio.create_task(finished())
            await task
            app.state.polling_task = task
            endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/healthz")
            return await endpoint()

        response = asyncio.run(scenario())

        self.assertEqual(response.status_code, 503)
        self.assertIn("polling task has stopped", json.loads(response.body)["reason"])

    def test_503_when_polling_is_stalled(self):
        app = create_app()
        app.state.ctx.health = PollHealth(stale_after_seconds=-1)

        response = self.call_healthz(app)

        self.assertEqual(response.status_code, 503)


class PollOnceObservabilityTests(unittest.TestCase):
    def test_successful_poll_beats_and_stamps_the_metric(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.return_value = [MagicMock(id="1")]

        poll_once(ctx)

        self.assertEqual(ctx.health.beat.call_count, 2)
        self.assertGreater(Metrics.last_poll_timestamp._value.get(), 0)

    def test_each_poll_gives_the_store_a_chance_to_prune(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.side_effect = RuntimeError("down")

        poll_once(ctx)

        ctx.prune_if_due.assert_called_once_with()

    def test_each_poll_backs_up_before_pruning(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.return_value = []

        poll_once(ctx)

        maintenance = [call[0] for call in ctx.mock_calls if call[0].endswith("_if_due")]
        self.assertEqual(
            maintenance, ["backup_if_due", "prune_if_due", "send_put_forward_list_if_due"]
        )

    def test_health_is_checked_at_each_poll_even_when_the_fetch_fails(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.side_effect = RuntimeError("gmail down")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            poll_once(ctx)

        ctx.check_health.assert_called_once_with()

    def test_failing_email_is_counted_and_still_beats(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.return_value = [MagicMock(id="1")]
        ctx.process_email.side_effect = RuntimeError("boom")
        before = Metrics.emails_skipped._value.get()

        poll_once(ctx)

        self.assertEqual(Metrics.emails_skipped._value.get(), before + 1)
        self.assertEqual(ctx.health.beat.call_count, 2)

    def test_failed_fetch_still_beats_so_a_gmail_outage_never_triggers_the_watchdog(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.side_effect = RuntimeError("down")
        before = Metrics.last_poll_timestamp._value.get()

        poll_once(ctx)

        ctx.health.beat.assert_called_once()
        self.assertEqual(Metrics.last_poll_timestamp._value.get(), before)

    def test_failed_fetch_is_counted_and_reported_to_the_outage_notifier(self):
        ctx = make_ctx()
        error = RuntimeError("down")
        ctx.mail.fetch_unread.side_effect = error
        before = Metrics.poll_failures._value.get()

        poll_once(ctx)

        self.assertEqual(Metrics.poll_failures._value.get(), before + 1)
        ctx.outage.record_failure.assert_called_once_with(error)
        ctx.outage.record_success.assert_not_called()

    def test_successful_fetch_clears_the_outage_even_with_no_mail(self):
        ctx = make_ctx()
        ctx.mail.fetch_unread.return_value = []

        poll_once(ctx)

        ctx.outage.record_success.assert_called_once_with()
        ctx.outage.record_failure.assert_not_called()

    def test_stop_request_ends_the_batch_between_emails(self):
        ctx = make_ctx()
        ctx.stopping.is_set.side_effect = [False, True]
        ctx.mail.fetch_unread.return_value = [MagicMock(id="1"), MagicMock(id="2")]

        poll_once(ctx)

        ctx.process_email.assert_called_once()

    def test_history_sync_beats_after_each_email_and_stops_on_request(self):
        ctx = make_ctx()
        ctx.mail.fetch_history.return_value = [MagicMock(id="1"), MagicMock(id="2"), MagicMock(id="3")]
        ctx.stopping.is_set.side_effect = [False, False, True]

        sync_history_once(ctx)

        self.assertEqual(ctx.process_email.call_count, 2)
        self.assertEqual(ctx.health.beat.call_count, 2)


    def test_history_sync_skips_a_failing_email_and_goes_on(self):
        ctx = make_ctx()
        ctx.mail.fetch_history.return_value = [MagicMock(id="1"), MagicMock(id="2")]
        ctx.process_email.side_effect = [RuntimeError("boom"), None]
        before = Metrics.emails_skipped._value.get()

        with self.assertLogs("gmail-assistant", level="ERROR"):
            sync_history_once(ctx)

        self.assertEqual(ctx.process_email.call_count, 2)
        self.assertEqual(Metrics.emails_skipped._value.get(), before + 1)
        self.assertEqual(ctx.health.beat.call_count, 2)

    def test_history_that_cannot_be_fetched_does_not_stop_the_startup(self):
        ctx = make_ctx()
        ctx.mail.fetch_history.side_effect = RuntimeError("gmail down")

        with self.assertLogs("gmail-assistant", level="ERROR"):
            sync_history_once(ctx)

        ctx.process_email.assert_not_called()


class RouteMetricsTests(unittest.TestCase):
    def test_mark_route_counts_route_and_observes_confidence(self):
        before = Metrics.routes.labels(route="label")._value.get()

        Metrics.mark_route("label", "low", 0.42)

        self.assertEqual(Metrics.routes.labels(route="label")._value.get(), before + 1)


if __name__ == "__main__":
    unittest.main()
