import logging
import re

import requests

from src.domain import Button
from src.errors import ConfigurationError
from src.formatting import truncate
from src.ports import ChannelDeliveryError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10
DISCORD_MESSAGE_LIMIT = 2000
DISCORD_URL_HINT = (
    "Expected https://discord.com/api/webhooks/<id>/<token>; "
    "recreate the webhook if the token is lost."
)
DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$"
)


class DiscordChannel:
    name = "discord"
    interactive = False

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url
        self._url_valid = bool(DISCORD_WEBHOOK_RE.match(webhook_url))
        if webhook_url and not self._url_valid:
            logger.error(
                "DISCORD_WEBHOOK_URL is malformed; Discord alerts are disabled. %s",
                DISCORD_URL_HINT,
            )

    @property
    def is_configured(self) -> bool:
        return self._url_valid

    @property
    def has_webhook_url(self) -> bool:
        """True even for a malformed URL, so the startup probe can report it."""
        return bool(self._webhook_url)

    def check_connection(self) -> str:
        if not self._url_valid:
            raise ConfigurationError(f"webhook URL malformed. {DISCORD_URL_HINT}")
        response = requests.get(self._webhook_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return "webhook reachable"

    def update(
        self, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        raise ChannelDeliveryError("Discord webhook messages cannot be updated")

    def send(self, text: str, buttons: list[list[Button]] | None = None) -> None:
        if not self._url_valid:
            raise ChannelDeliveryError("Discord webhook URL malformed")
        try:
            response = requests.post(
                self._webhook_url,
                json={"content": truncate(text, DISCORD_MESSAGE_LIMIT)},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # requests embeds the URL, and with it the webhook token, in its messages.
            status = exc.response.status_code if exc.response is not None else "n/a"
            raise ChannelDeliveryError(
                f"Discord webhook failed: {type(exc).__name__} (status {status})"
            ) from None
