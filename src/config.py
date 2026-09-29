from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_user_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            user_id = int(part)
        except ValueError:
            raise ValueError(f"not an integer Telegram user id: {part!r}") from None
        if user_id <= 0:
            raise ValueError(f"Telegram user ids are positive, got {user_id}")
        ids.add(user_id)
    return frozenset(ids)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_client_id: str = ""
    google_client_secret: str = ""
    gmail_refresh_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    gemini_max_rpm: int = 12
    user_display_name: str = ""
    jev_api_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""

    startup_checks: str = "warn"
    poll_interval_seconds: int = 60
    low_confidence_threshold: float = 0.50
    watchdog_enabled: bool = True
    fetch_max_age_days: int = 3
    fetch_query: str = ""
    sync_history: bool = False
    gmail_user_id: str = "me"
    db_path: str = "data/assistant.db"
    # Only one process may long-poll a bot token; a second poller gets HTTP 409 from Telegram.
    telegram_inbound_enabled: bool = False
    # Comma-separated; mandatory for inbound in group chats, where anyone could press buttons.
    telegram_allowed_user_ids: str = ""
    jev_few_shot_enabled: bool = False

    mail_provider: str = "gmail"
    classifier: str = "jev"
    llm_provider: str = "gemini"
    # Comma-separated; the order is the delivery priority.
    alert_channels: str = "telegram,discord"
    chat_inbox: str = "telegram"
    store_backend: str = "sqlite"

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _validate_user_ids(cls, value: str) -> str:
        parse_user_ids(value)
        return value

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        return parse_user_ids(self.telegram_allowed_user_ids)

    @property
    def alert_channel_names(self) -> list[str]:
        return [name.strip() for name in self.alert_channels.split(",") if name.strip()]

    @property
    def poll_stale_after_seconds(self) -> int:
        # Generous: a single mail can wait on LLM rate limiting and retries for a few minutes.
        return max(5 * self.poll_interval_seconds, 600)
