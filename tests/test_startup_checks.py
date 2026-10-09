import logging
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.domain import EmailMessage
from src.gateways import AlertGateway
from src.gateways.discord import DiscordChannel
from src.gateways.telegram_bot import TelegramBot, TelegramChannel
from src.gmail.client import GmailClient
from src.health import StartupCheckError, StartupCheckMode, run_startup_checks
from src.triage import HeuristicClassifier, JevClassifier

VALID_DISCORD_URL = "https://discord.com/api/webhooks/123/abc-DEF_1"


def http_error(status: int, url: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} for url: {url}", response=response)


def telegram_channel(*responses, chat_id="42") -> tuple[TelegramChannel, MagicMock]:
    http = MagicMock()
    http.post.side_effect = list(responses)
    return TelegramChannel(TelegramBot("tok", chat_id, http=http)), http


def telegram_gateway(*responses, discord_url="") -> tuple[AlertGateway, MagicMock]:
    channel, http = telegram_channel(*responses)
    return AlertGateway([channel, DiscordChannel(discord_url)]), http


def telegram_ok(result) -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": True, "result": result}
    return response


class RunStartupChecksTests(unittest.TestCase):
    def test_reports_ok_failed_and_skipped(self):
        def broken():
            raise RuntimeError("boom")

        results = run_startup_checks(
            {"gmail": lambda: "fine", "jev": broken, "discord": None}, StartupCheckMode.WARN
        )

        self.assertEqual([(r.name, r.status) for r in results], [("gmail", "ok"), ("jev", "failed"), ("discord", "skipped")])

    def test_off_runs_nothing(self):
        probe = MagicMock()

        self.assertEqual(run_startup_checks({"gmail": probe}, StartupCheckMode.OFF), [])
        probe.assert_not_called()

    def test_strict_raises_on_failure_but_not_on_skipped(self):
        def broken():
            raise RuntimeError("boom")

        with self.assertRaises(StartupCheckError) as ctx:
            run_startup_checks({"gmail": broken, "discord": None}, StartupCheckMode.STRICT)

        self.assertIn("gmail", str(ctx.exception))
        self.assertNotIn("discord", str(ctx.exception))
        run_startup_checks({"discord": None}, StartupCheckMode.STRICT)

    def test_warn_does_not_raise_on_failure(self):
        def broken():
            raise RuntimeError("boom")

        run_startup_checks({"gmail": broken}, StartupCheckMode.WARN)

    def test_failure_detail_never_leaks_secret_url(self):
        def broken():
            raise http_error(401, "https://api.telegram.org/botSECRET-TOKEN/getMe")

        with self.assertLogs("src.health.connections", level=logging.ERROR) as logs:
            results = run_startup_checks({"telegram": broken}, StartupCheckMode.WARN)

        self.assertEqual(results[0].detail, "HTTPError (HTTP 401)")
        self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))

    def test_logs_each_result_with_its_level(self):
        with self.assertLogs("src.health.connections", level=logging.INFO) as logs:
            run_startup_checks({"gmail": lambda: "fine", "discord": None}, StartupCheckMode.WARN)

        self.assertEqual([r.levelname for r in logs.records], ["INFO", "WARNING"])


class ClientProbeTests(unittest.TestCase):
    def test_gmail_probe_returns_authenticated_account(self):
        client = GmailClient(client_id="", client_secret="", refresh_token="")
        self.assertFalse(client.is_configured)
        client._service = MagicMock()
        client._service.users().getProfile().execute.return_value = {"emailAddress": "me@example.com"}

        self.assertTrue(client.is_configured)
        self.assertEqual(client.check_connection(), "authenticated as me@example.com")

    def test_jev_probe_sends_bearer_key_and_raises_on_rejection(self):
        client = JevClassifier(api_url="https://jev.example", api_key="k")
        response = MagicMock()
        response.raise_for_status.side_effect = http_error(401, "https://jev.example")

        with (
            patch("src.triage.engine.requests.post", return_value=response) as post,
            self.assertRaises(requests.HTTPError),
        ):
            client.check_connection()

        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer k"})

    def test_telegram_probe_uses_get_me_without_sending_message(self):
        channel, http = telegram_channel(telegram_ok({"username": "mybot"}), chat_id="1")

        detail = channel.check_connection()

        self.assertEqual(http.post.call_args.args[0], "https://api.telegram.org/bottok/getMe")
        self.assertEqual(detail, "bot @mybot reachable")

    def test_telegram_probe_failure_never_carries_the_token(self):
        channel, _ = telegram_channel(
            requests.ConnectionError("Max retries with url: /bottok/getMe"), chat_id="1"
        )

        results = run_startup_checks({"telegram": channel.check_connection}, StartupCheckMode.WARN)

        self.assertEqual(results[0].status, "failed")
        self.assertNotIn("tok", results[0].detail)


if __name__ == "__main__":
    unittest.main()


class FormatStatusReportTests(unittest.TestCase):
    def test_formats_greeting_and_one_line_per_check(self):
        from src.health import ConnectionCheck, format_status_report

        checks = [
            ConnectionCheck("gmail", "ok", "authenticated as me@example.com"),
            ConnectionCheck("discord", "failed", "HTTPError (HTTP 401)"),
            ConnectionCheck("jev", "skipped", "not configured"),
        ]

        report = format_status_report(checks)

        self.assertTrue(report.startswith("👋 Bonjour, je suis ton assistant Gmail. Je viens de démarrer."))
        self.assertIn("✅ gmail: authenticated as me@example.com", report)
        self.assertIn("❌ discord: HTTPError (HTTP 401)", report)
        self.assertIn("⏭️ jev: not configured", report)


class SendTextTests(unittest.TestCase):
    def test_sends_to_configured_chat(self):
        gateway, http = telegram_gateway(telegram_ok({"message_id": 1}))

        gateway.send_text("hello")

        self.assertEqual(http.post.call_args.args[0], "https://api.telegram.org/bottok/sendMessage")
        payload = http.post.call_args.kwargs["json"]
        self.assertEqual((payload["chat_id"], payload["text"]), ("42", "hello"))

    def test_never_posts_to_discord(self):
        gateway, http = telegram_gateway(telegram_ok({"message_id": 1}), discord_url=VALID_DISCORD_URL)

        with patch("src.gateways.discord.requests.post") as post:
            gateway.send_text("hello")

        post.assert_not_called()
        self.assertEqual(http.post.call_count, 1)

    def test_telegram_api_error_status_is_caught_and_logged_not_raised(self):
        response = MagicMock()
        response.raise_for_status.side_effect = http_error(403, "https://api.telegram.org/bottok/sendMessage")
        gateway, _ = telegram_gateway(response)

        with self.assertLogs("src.gateways.alerts", level=logging.WARNING) as logs:
            gateway.send_text("hello")  # must not raise

        self.assertIn("telegram", logs.output[0])
        self.assertIn("status 403", logs.output[0])
        self.assertNotIn("bottok", "\n".join(logs.output))

    def test_urgent_alert_sends_discord_even_if_telegram_fails(self):
        telegram_response = MagicMock()
        telegram_response.raise_for_status.side_effect = http_error(500, "https://api.telegram.org/bottok/sendMessage")
        gateway, http = telegram_gateway(telegram_response, discord_url=VALID_DISCORD_URL)
        discord_response = MagicMock()
        email = EmailMessage(id="1", thread_id="t1", sender="a@b.com", subject="s", snippet="s", body="")
        triage = HeuristicClassifier().classify(email)

        with patch("src.gateways.discord.requests.post", return_value=discord_response) as post:
            gateway.send_urgent_alert(email, triage)  # must not raise

        self.assertEqual(http.post.call_count, 1)
        self.assertEqual(post.call_count, 1)
