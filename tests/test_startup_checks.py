import logging
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.gateways import AlertGateway
from src.gmail.client import GmailClient
from src.health import StartupCheckError, StartupCheckMode, run_startup_checks
from src.triage.engine import DecisionEngineClient


def http_error(status: int, url: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} for url: {url}", response=response)


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
        client = DecisionEngineClient(api_url="https://jev.example", api_key="k")
        response = MagicMock()
        response.raise_for_status.side_effect = http_error(401, "https://jev.example")

        with (
            patch("src.triage.engine.requests.post", return_value=response) as post,
            self.assertRaises(requests.HTTPError),
        ):
            client.check_connection()

        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer k"})

    def test_telegram_probe_uses_get_me_without_sending_message(self):
        gateway = AlertGateway(telegram_bot_token="tok", telegram_chat_id="1")
        response = MagicMock()
        response.json.return_value = {"result": {"username": "mybot"}}

        with patch("src.gateways.alerts.requests.get", return_value=response) as get:
            detail = gateway.check_telegram()

        self.assertEqual(get.call_args.args[0], "https://api.telegram.org/bottok/getMe")
        self.assertEqual(detail, "bot @mybot reachable")

    def test_discord_probe_reads_webhook_without_posting(self):
        gateway = AlertGateway(discord_webhook_url="https://discord.example/hook")

        with patch("src.gateways.alerts.requests.get", return_value=MagicMock()) as get, patch(
            "src.gateways.alerts.requests.post"
        ) as post:
            gateway.check_discord()

        get.assert_called_once()
        post.assert_not_called()

    def test_alert_gateway_configuration_flags(self):
        self.assertFalse(AlertGateway().telegram_configured)
        self.assertFalse(AlertGateway(telegram_bot_token="t").telegram_configured)
        self.assertTrue(AlertGateway(telegram_bot_token="t", telegram_chat_id="1").telegram_configured)
        self.assertTrue(AlertGateway(discord_webhook_url="u").discord_configured)


class ConnectionProbesTests(unittest.TestCase):
    def test_unconfigured_services_are_skipped(self):
        from main import ApplicationContext
        from src.config import Settings

        settings = Settings(_env_file=None, jev_api_key="", gemini_api_key="", google_client_id="")
        probes = ApplicationContext(settings).connection_probes()

        self.assertEqual(set(probes), {"gmail", "gemini", "jev", "telegram", "discord"})
        self.assertTrue(all(probe is None for probe in probes.values()))


if __name__ == "__main__":
    unittest.main()
