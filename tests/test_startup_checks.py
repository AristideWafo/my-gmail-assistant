import logging
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.domain import EmailMessage
from src.gateways import AlertGateway
from src.gateways.telegram_bot import TelegramBot
from src.gmail.client import GmailClient
from src.health import StartupCheckError, StartupCheckMode, run_startup_checks
from src.triage import HeuristicClassifier, JevClassifier

VALID_DISCORD_URL = "https://discord.com/api/webhooks/123/abc-DEF_1"


def http_error(status: int, url: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} for url: {url}", response=response)


def telegram_gateway(*responses, chat_id="42", **kwargs) -> tuple[AlertGateway, MagicMock]:
    http = MagicMock()
    http.post.side_effect = list(responses)
    return AlertGateway(telegram=TelegramBot("tok", chat_id, http=http), **kwargs), http


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
        gateway, http = telegram_gateway(telegram_ok({"username": "mybot"}), chat_id="1")

        detail = gateway.check_telegram()

        self.assertEqual(http.post.call_args.args[0], "https://api.telegram.org/bottok/getMe")
        self.assertEqual(detail, "bot @mybot reachable")

    def test_telegram_probe_failure_never_carries_the_token(self):
        http = MagicMock()
        http.post.side_effect = requests.ConnectionError("Max retries with url: /bottok/getMe")
        gateway = AlertGateway(telegram=TelegramBot("tok", "1", http=http))

        results = run_startup_checks({"telegram": gateway.check_telegram}, StartupCheckMode.WARN)

        self.assertEqual(results[0].status, "failed")
        self.assertNotIn("tok", results[0].detail)

    def test_discord_probe_reads_webhook_without_posting(self):
        gateway = AlertGateway(discord_webhook_url=VALID_DISCORD_URL)

        with patch("src.gateways.alerts.requests.get", return_value=MagicMock()) as get, patch(
            "src.gateways.alerts.requests.post"
        ) as post:
            gateway.check_discord()

        get.assert_called_once()
        post.assert_not_called()

    def test_alert_gateway_configuration_flags(self):
        self.assertFalse(AlertGateway().telegram_configured)
        self.assertFalse(AlertGateway(telegram=TelegramBot("t", "")).telegram_configured)
        self.assertTrue(AlertGateway(telegram=TelegramBot("t", "1")).telegram_configured)
        self.assertTrue(AlertGateway(discord_webhook_url="u").discord_configured)


class ConnectionProbesTests(unittest.TestCase):
    def test_unconfigured_services_are_skipped(self):
        from main import ApplicationContext
        from src.config import Settings

        settings = Settings(
            _env_file=None, jev_api_key="", gemini_api_key="", google_client_id="", db_path=":memory:"
        )
        probes = ApplicationContext(settings).connection_probes()

        self.assertEqual(set(probes), {"gmail", "gemini", "jev", "telegram", "discord"})
        self.assertTrue(all(probe is None for probe in probes.values()))


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


class SendTelegramTextTests(unittest.TestCase):
    def test_sends_to_configured_chat(self):
        gateway, http = telegram_gateway(telegram_ok({"message_id": 1}))

        gateway.send_telegram_text("hello")

        self.assertEqual(http.post.call_args.args[0], "https://api.telegram.org/bottok/sendMessage")
        self.assertEqual(http.post.call_args.kwargs["json"], {"chat_id": "42", "text": "hello"})

    def test_noop_when_not_configured(self):
        with patch("src.gateways.alerts.requests.post") as post:
            AlertGateway().send_telegram_text("hello")

        post.assert_not_called()

    def test_telegram_api_error_status_is_caught_and_logged_not_raised(self):
        response = MagicMock()
        response.raise_for_status.side_effect = http_error(403, "https://api.telegram.org/bottok/sendMessage")
        gateway, _ = telegram_gateway(response)

        with self.assertLogs("src.gateways.alerts", level=logging.WARNING) as logs:
            gateway.send_telegram_text("hello")  # must not raise

        self.assertIn("telegram", logs.output[0])
        self.assertIn("status 403", logs.output[0])
        self.assertNotIn("bottok", "\n".join(logs.output))

    def test_urgent_alert_sends_discord_even_if_telegram_fails(self):
        telegram_response = MagicMock()
        telegram_response.raise_for_status.side_effect = http_error(500, "https://api.telegram.org/bottok/sendMessage")
        gateway, http = telegram_gateway(telegram_response, discord_webhook_url=VALID_DISCORD_URL)
        discord_response = MagicMock()
        email = EmailMessage(id="1", thread_id="t1", sender="a@b.com", subject="s", snippet="s", body="")
        triage = HeuristicClassifier().classify(email)

        with patch("src.gateways.alerts.requests.post", return_value=discord_response) as post:
            gateway.send_urgent_alert(email, triage)  # must not raise

        self.assertEqual(http.post.call_count, 1)
        self.assertEqual(post.call_count, 1)
