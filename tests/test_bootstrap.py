import unittest

from main import ApplicationContext
from src.bootstrap import build_components, connection_probes
from src.config import Settings
from src.domain import EmailMessage, TriageResult
from src.errors import ConfigurationError
from src.gateways.discord import DiscordChannel
from src.gateways.telegram_bot import TelegramBot, TelegramChannel
from src.gmail import GmailClient
from src.llm import GeminiClient
from src.storage import SqliteDecisionStore
from src.triage import FallbackClassifier, HeuristicClassifier, JevClassifier
from tests.fakes import FakeChannel, FakeClassifier, fake_components


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{"db_path": ":memory:", **overrides})


def build(**overrides):
    return build_components(make_settings(**overrides))


class DefaultSelectionTests(unittest.TestCase):
    def test_defaults_build_todays_adapters(self):
        components = build()

        self.assertIsInstance(components.store, SqliteDecisionStore)
        self.assertIsInstance(components.mail, GmailClient)
        self.assertIsInstance(components.classifier, FallbackClassifier)
        self.assertIsInstance(components.classifier.primary, JevClassifier)
        self.assertIsInstance(components.classifier.secondary, HeuristicClassifier)
        self.assertIsInstance(components.analyzer, GeminiClient)
        self.assertEqual(
            [type(channel) for channel in components.channels], [TelegramChannel, DiscordChannel]
        )
        self.assertIsInstance(components.chat, TelegramBot)

    def test_telegram_channel_and_chat_inbox_share_one_bot(self):
        components = build(telegram_bot_token="t", telegram_chat_id="1")

        self.assertIs(components.channels[0]._bot, components.chat)

    def test_settings_reach_the_adapters(self):
        components = build(
            telegram_bot_token="t", telegram_chat_id="-100", telegram_allowed_user_ids="7"
        )

        self.assertTrue(components.chat.is_configured)
        self.assertTrue(components.chat.inbound_authorized)


class SelectorTests(unittest.TestCase):
    def test_heuristic_classifier_alone(self):
        self.assertIsInstance(build(classifier="heuristic").classifier, HeuristicClassifier)

    def test_channel_list_selects_and_orders_channels(self):
        self.assertEqual([c.name for c in build(alert_channels="discord").channels], ["discord"])
        self.assertEqual(
            [c.name for c in build(alert_channels=" discord , telegram ").channels],
            ["discord", "telegram"],
        )

    def test_chat_none_disables_the_inbox_and_feedback_buttons(self):
        settings = make_settings(
            chat_inbox="none",
            telegram_inbound_enabled=True,
            telegram_bot_token="t",
            telegram_chat_id="1",
        )
        self.assertIsNone(build_components(settings).chat)

        with self.assertLogs("gmail-assistant", level="WARNING"):
            ctx = ApplicationContext(settings)

        self.assertFalse(ctx.inbound_enabled)
        self.assertFalse(ctx.alerts._feedback_buttons)

    def test_unknown_values_are_rejected_with_the_valid_choices(self):
        cases = {
            "mail_provider": ("outlook", "gmail"),
            "classifier": ("gpt", "heuristic, jev"),
            "llm_provider": ("claude", "gemini"),
            "alert_channels": ("telegram,slack", "discord, telegram"),
            "chat_inbox": ("discord", "none, telegram"),
            "store_backend": ("postgres", "sqlite"),
        }
        for field, (value, choices) in cases.items():
            with self.subTest(field=field), self.assertRaises(ConfigurationError) as raised:
                build(**{field: value})
            self.assertIn(repr(value.split(",")[-1]), str(raised.exception))
            self.assertIn(f"valid choices: {choices}", str(raised.exception))

    def test_empty_or_duplicated_channel_list_is_rejected(self):
        for value in ("", " , ", "telegram,telegram"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                build(alert_channels=value)


class FewShotWiringTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertIsNone(build().classifier.primary.examples_provider)

    def test_enabled_provider_reads_corrections_from_the_store(self):
        components = build(jev_few_shot_enabled=True)
        email = EmailMessage(
            id="m1", thread_id="t1", sender="a@b.com", subject="Hi", snippet="s", body="b"
        )
        components.store.record_decision(email, TriageResult("high", "personnel", 0.9), "llm")
        components.store.record_feedback("m1", "false_urgent")

        examples = components.classifier.primary.examples_provider()

        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]["sender_domain"], "b.com")
        self.assertEqual(examples[0]["correct_urgency"], "medium")


class NeedsReplyWiringTests(unittest.TestCase):
    def test_question_is_off_by_default_and_follows_the_flag(self):
        self.assertFalse(build().classifier.primary.ask_needs_reply)
        self.assertTrue(build(needs_reply_enabled=True).classifier.primary.ask_needs_reply)


class AttentionWiringTests(unittest.TestCase):
    def test_questions_are_off_by_default(self):
        self.assertFalse(build().classifier.primary.ask_attention)

    def test_both_modes_ask_the_attention_questions_and_whether_a_reply_is_expected(self):
        for mode in ("shadow", "on"):
            with self.subTest(mode=mode):
                classifier = build(attention_mode=mode).classifier.primary

                self.assertTrue(classifier.ask_attention)
                self.assertTrue(classifier.ask_needs_reply)


class ConnectionProbesTests(unittest.TestCase):
    def test_unconfigured_defaults_are_all_skipped_under_todays_names(self):
        probes = connection_probes(build(jev_api_key="", gemini_api_key="", google_client_id=""))

        self.assertEqual(list(probes), ["gmail", "gemini", "jev", "telegram", "discord"])
        self.assertTrue(all(probe is None for probe in probes.values()))

    def test_configured_channels_are_probed_including_a_malformed_discord_url(self):
        with self.assertLogs("src.gateways.discord", level="ERROR"):
            components = build(
                telegram_bot_token="t",
                telegram_chat_id="1",
                discord_webhook_url="https://discord.com/api/webhooks/123",
            )
        telegram, discord = components.channels

        probes = connection_probes(components)

        self.assertEqual(probes["telegram"], telegram.check_connection)
        self.assertEqual(probes["discord"], discord.check_connection)

    def test_jev_is_probed_only_when_its_key_is_set(self):
        components = build(jev_api_key="k")

        self.assertEqual(
            connection_probes(components)["jev"], components.classifier.check_connection
        )

    def test_chat_inbox_is_probed_when_no_alert_channel_shares_its_client(self):
        components = build(
            alert_channels="discord", telegram_bot_token="t", telegram_chat_id="1"
        )

        probes = connection_probes(components)

        self.assertEqual(probes["telegram"], components.chat.check_connection)

    def test_chat_inbox_is_not_probed_twice_alongside_its_alert_channel(self):
        probes = connection_probes(build(telegram_bot_token="t", telegram_chat_id="1"))

        self.assertEqual(list(probes).count("telegram"), 1)

    def test_probes_follow_the_selection(self):
        probes = connection_probes(build(classifier="heuristic", alert_channels="discord"))

        self.assertEqual(list(probes), ["gmail", "gemini", "heuristic", "discord", "telegram"])
        self.assertIsNotNone(probes["heuristic"])

    def test_injected_components_are_gated_on_is_configured(self):
        ready, missing = FakeChannel(name="ready"), FakeChannel(name="missing", is_configured=False)
        components = fake_components(
            probe_targets=(("ready", ready), ("missing", missing), ("cls", FakeClassifier()))
        )

        probes = connection_probes(components)

        self.assertEqual(probes["ready"](), "ready reachable")
        self.assertIsNone(probes["missing"])
        self.assertEqual(probes["cls"](), "fake classifier")


if __name__ == "__main__":
    unittest.main()
