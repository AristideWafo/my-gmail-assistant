import unittest
from unittest.mock import MagicMock, patch

import requests

from src.gateways.discord import DiscordChannel
from src.health import StartupCheckMode, run_startup_checks
from src.ports import AlertChannel, ChannelDeliveryError

VALID_URL = "https://discord.com/api/webhooks/123/abc-DEF_1"


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} for url: {VALID_URL}", response=response)


class ConfigurationTests(unittest.TestCase):
    def test_is_a_non_interactive_alert_channel(self):
        channel = DiscordChannel(VALID_URL)

        self.assertIsInstance(channel, AlertChannel)
        self.assertEqual(channel.name, "discord")
        self.assertFalse(channel.interactive)

    def test_valid_url_forms_are_configured(self):
        for url in (
            VALID_URL,
            "https://discordapp.com/api/webhooks/1/tok",
            "https://canary.discord.com/api/webhooks/1/tok/",
        ):
            with self.subTest(url=url):
                self.assertTrue(DiscordChannel(url).is_configured)

    def test_empty_url_is_neither_configured_nor_probed(self):
        channel = DiscordChannel("")

        self.assertFalse(channel.is_configured)
        self.assertFalse(channel.has_webhook_url)

    def test_malformed_url_is_logged_once_and_not_configured_but_still_probed(self):
        with self.assertLogs("src.gateways.discord", level="ERROR") as logs:
            channel = DiscordChannel("https://discord.com/api/webhooks/123456789")

        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("123456789", logs.output[0])
        self.assertFalse(channel.is_configured)
        self.assertTrue(channel.has_webhook_url)


class SendTests(unittest.TestCase):
    def test_posts_content_and_ignores_buttons(self):
        with patch("src.gateways.discord.requests.post") as post:
            result = DiscordChannel(VALID_URL).send("hello", buttons=[[("Ok", "fb:v:1")]])

        self.assertIsNone(result)
        self.assertEqual(post.call_args.args[0], VALID_URL)
        self.assertEqual(post.call_args.kwargs["json"], {"content": "hello"})

    def test_content_is_truncated_to_the_2000_character_limit(self):
        with patch("src.gateways.discord.requests.post") as post:
            DiscordChannel(VALID_URL).send("x" * 3500)

        self.assertLessEqual(len(post.call_args.kwargs["json"]["content"]), 2000)

    def assert_sanitized(self, post_patch: dict, expected: str) -> None:
        with (
            patch("src.gateways.discord.requests.post", **post_patch),
            self.assertRaises(ChannelDeliveryError) as ctx,
        ):
            DiscordChannel(VALID_URL).send("hello")

        self.assertEqual(str(ctx.exception), f"Discord webhook failed: {expected}")
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_connection_failure_never_carries_the_webhook_token(self):
        error = requests.ConnectionError(f"Max retries exceeded with url: {VALID_URL}")

        self.assert_sanitized({"side_effect": error}, "ConnectionError (status n/a)")

    def test_http_failure_keeps_only_type_and_status(self):
        response = MagicMock()
        response.raise_for_status.side_effect = http_error(404)

        self.assert_sanitized({"return_value": response}, "HTTPError (status 404)")

    def test_malformed_url_is_never_posted_to(self):
        with self.assertLogs("src.gateways.discord", level="ERROR"):
            channel = DiscordChannel("https://discord.com/api/webhooks/123")

        with (
            patch("src.gateways.discord.requests.post") as post,
            self.assertRaises(ChannelDeliveryError),
        ):
            channel.send("hello")

        post.assert_not_called()


class CheckConnectionTests(unittest.TestCase):
    def test_reads_the_webhook_without_posting(self):
        with (
            patch("src.gateways.discord.requests.get", return_value=MagicMock()) as get,
            patch("src.gateways.discord.requests.post") as post,
        ):
            self.assertEqual(DiscordChannel(VALID_URL).check_connection(), "webhook reachable")

        get.assert_called_once()
        post.assert_not_called()

    def test_malformed_url_fails_the_probe_with_the_hint_without_leaking_it(self):
        with self.assertLogs("src.gateways.discord", level="ERROR"):
            channel = DiscordChannel("https://discord.com/api/webhooks/123456789")

        results = run_startup_checks({"discord": channel.check_connection}, StartupCheckMode.WARN)

        self.assertEqual(results[0].status, "failed")
        self.assertIn("malformed", results[0].detail)
        self.assertIn("recreate the webhook", results[0].detail)
        self.assertNotIn("123456789", results[0].detail)


if __name__ == "__main__":
    unittest.main()
