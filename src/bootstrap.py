from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any, TypeVar

from src.config import Settings
from src.errors import ConfigurationError
from src.gateways.discord import DiscordChannel
from src.gateways.telegram_bot import TelegramBot, TelegramChannel
from src.gateways.unsubscribe_http import HttpUnsubscriber
from src.gmail.client import GmailClient, build_unread_query
from src.llm.gemini import GeminiClient
from src.observability.metrics import Metrics
from src.ports import (
    AlertChannel,
    ChatInbox,
    DecisionStore,
    EmailAnalyzer,
    EmailClassifier,
    MailProvider,
    Unsubscriber,
)
from src.storage.decision_store import SqliteDecisionStore
from src.triage.engine import JEV_RECOVERABLE_ERRORS, JevClassifier
from src.triage.fallback import FallbackClassifier
from src.triage.few_shot import EXAMPLE_VERDICTS, MAX_EXAMPLES, build_examples
from src.triage.heuristic import HeuristicClassifier

T = TypeVar("T")
Probe = Callable[[], str]


class BuildContext:
    """What factories may depend on: settings, the already-built store and shared clients."""

    def __init__(self, settings: Settings, store: DecisionStore) -> None:
        self.settings = settings
        self.store = store

    @cached_property
    def telegram_bot(self) -> TelegramBot:
        # One instance for both the alert channel and the chat inbox: the inbox's allowlist and
        # HTTP session must be the ones the alerts were sent with.
        return TelegramBot(
            self.settings.telegram_bot_token,
            self.settings.telegram_chat_id,
            allowed_user_ids=self.settings.allowed_user_ids,
        )


Factory = Callable[[BuildContext], T]


def _gmail(ctx: BuildContext) -> MailProvider:
    s = ctx.settings
    return GmailClient(
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        refresh_token=s.gmail_refresh_token,
        user_id=s.gmail_user_id,
        unread_query=s.fetch_query or build_unread_query(s.fetch_max_age_days),
    )


def _jev(ctx: BuildContext) -> EmailClassifier:
    s = ctx.settings
    store = ctx.store
    examples_provider = (
        (lambda: build_examples(store.recent_corrections(MAX_EXAMPLES, EXAMPLE_VERDICTS)))
        if s.jev_few_shot_enabled
        else None
    )
    return FallbackClassifier(
        JevClassifier(s.jev_api_url, s.jev_api_key, examples_provider=examples_provider),
        HeuristicClassifier(),
        recoverable=JEV_RECOVERABLE_ERRORS,
        on_fallback=Metrics.mark_jev_fallback,
    )


def _gemini(ctx: BuildContext) -> EmailAnalyzer:
    s = ctx.settings
    return GeminiClient(
        s.gemini_api_key, s.gemini_model, max_rpm=s.gemini_max_rpm, user_name=s.user_display_name
    )


STORES: dict[str, Callable[[Settings], DecisionStore]] = {
    "sqlite": lambda settings: SqliteDecisionStore(settings.db_path),
}
MAIL_PROVIDERS: dict[str, Factory[MailProvider]] = {"gmail": _gmail}
CLASSIFIERS: dict[str, Factory[EmailClassifier]] = {
    "jev": _jev,
    "heuristic": lambda ctx: HeuristicClassifier(),
}
ANALYZERS: dict[str, Factory[EmailAnalyzer]] = {"gemini": _gemini}
ALERT_CHANNELS: dict[str, Factory[AlertChannel]] = {
    "telegram": lambda ctx: TelegramChannel(ctx.telegram_bot),
    "discord": lambda ctx: DiscordChannel(ctx.settings.discord_webhook_url),
}
CHAT_INBOXES: dict[str, Factory[ChatInbox | None]] = {
    "telegram": lambda ctx: ctx.telegram_bot,
    "none": lambda ctx: None,
}
UNSUBSCRIBERS: dict[str, Factory[Unsubscriber | None]] = {
    "http": lambda ctx: HttpUnsubscriber(),
    "none": lambda ctx: None,
}

# Default gate is `is_configured`. JEV sits behind a fallback that is always configured, and a
# malformed Discord URL must still be probed so startup reports it instead of skipping it.
PROBE_GATES: dict[str, Callable[[Any], bool]] = {
    "jev": lambda classifier: classifier.primary.is_configured,
    "discord": lambda channel: channel.has_webhook_url,
}


@dataclass(frozen=True)
class Components:
    mail: MailProvider
    classifier: EmailClassifier
    analyzer: EmailAnalyzer
    channels: tuple[AlertChannel, ...]
    chat: ChatInbox | None
    store: DecisionStore
    unsubscriber: Unsubscriber | None = None
    # (registry name, component) pairs to probe at startup, in report order.
    probe_targets: tuple[tuple[str, Any], ...] = ()


def _select(registry: Mapping[str, T], name: str, kind: str) -> T:
    try:
        return registry[name]
    except KeyError:
        choices = ", ".join(sorted(registry))
        raise ConfigurationError(f"unknown {kind} {name!r}; valid choices: {choices}") from None


def _channel_names(settings: Settings) -> list[str]:
    names = settings.alert_channel_names
    if not names:
        raise ConfigurationError("ALERT_CHANNELS must name at least one channel")
    if len(set(names)) != len(names):
        raise ConfigurationError(f"ALERT_CHANNELS lists a channel twice: {settings.alert_channels}")
    for name in names:
        _select(ALERT_CHANNELS, name, "ALERT_CHANNELS entry")
    return names


def build_components(settings: Settings) -> Components:
    # Resolve every selector before building anything, so a typo never leaves an open store behind.
    store_factory = _select(STORES, settings.store_backend, "STORE_BACKEND")
    mail_factory = _select(MAIL_PROVIDERS, settings.mail_provider, "MAIL_PROVIDER")
    classifier_factory = _select(CLASSIFIERS, settings.classifier, "CLASSIFIER")
    analyzer_factory = _select(ANALYZERS, settings.llm_provider, "LLM_PROVIDER")
    chat_factory = _select(CHAT_INBOXES, settings.chat_inbox, "CHAT_INBOX")
    unsubscriber_factory = _select(UNSUBSCRIBERS, settings.unsubscriber, "UNSUBSCRIBER")
    channel_names = _channel_names(settings)

    ctx = BuildContext(settings, store_factory(settings))
    mail = mail_factory(ctx)
    classifier = classifier_factory(ctx)
    analyzer = analyzer_factory(ctx)
    channels = [(name, ALERT_CHANNELS[name](ctx)) for name in channel_names]
    chat = chat_factory(ctx)
    return Components(
        mail=mail,
        classifier=classifier,
        analyzer=analyzer,
        channels=tuple(channel for _, channel in channels),
        chat=chat,
        store=ctx.store,
        unsubscriber=unsubscriber_factory(ctx),
        probe_targets=(
            (settings.mail_provider, mail),
            (settings.llm_provider, analyzer),
            (settings.classifier, classifier),
            *channels,
            *_chat_probe(settings, chat, channel_names),
        ),
    )


def _chat_probe(
    settings: Settings, chat: ChatInbox | None, channel_names: list[str]
) -> tuple[tuple[str, Any], ...]:
    # A same-named alert channel already probes the shared client; probe the inbox only alone.
    if chat is None or settings.chat_inbox in channel_names:
        return ()
    return ((settings.chat_inbox, chat),)


def connection_probes(components: Components) -> dict[str, Probe | None]:
    def gated(name: str, component: Any) -> Probe | None:
        gate = PROBE_GATES.get(name, lambda c: c.is_configured)
        return component.check_connection if gate(component) else None

    return {name: gated(name, component) for name, component in components.probe_targets}
