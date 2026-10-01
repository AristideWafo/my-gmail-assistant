from pydantic import Field, field_validator
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
    # 0 disables the chat message sent when fetching mail keeps failing.
    poll_failure_alert_minutes: int = Field(default=10, ge=0)
    low_confidence_threshold: float = 0.50
    # TOML file of extra deterministic rules and VIP senders; empty keeps the built-in rules only.
    triage_rules_path: str = ""
    watchdog_enabled: bool = True
    fetch_max_age_days: int = 3
    fetch_query: str = ""
    sync_history: bool = False
    gmail_user_id: str = "me"
    db_path: str = "data/assistant.db"
    # Empty disables backups.
    backup_dir: str = ""
    backup_keep: int = Field(default=7, ge=1)
    # Only one process may long-poll a bot token; a second poller gets HTTP 409 from Telegram.
    telegram_inbound_enabled: bool = False
    # Comma-separated; mandatory for inbound in group chats, where anyone could press buttons.
    telegram_allowed_user_ids: str = ""
    jev_few_shot_enabled: bool = False
    # Asks JEV whether a mail expects a reply and drafts one, whatever the urgency.
    needs_reply_enabled: bool = False
    needs_reply_threshold: float = Field(default=0.5, ge=0, le=1)
    # Offers to unsubscribe from senders whose mail is always archived; needs the chat inbox.
    unsubscribe_proposals_enabled: bool = False
    unsubscribe_min_archived: int = Field(default=5, ge=2)

    mail_provider: str = "gmail"
    classifier: str = "jev"
    llm_provider: str = "gemini"
    # Comma-separated; every listed channel is sent to, in this order (first interactive one wins replies).
    alert_channels: str = "telegram,discord"
    chat_inbox: str = "telegram"
    store_backend: str = "sqlite"
    unsubscriber: str = "http"

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
