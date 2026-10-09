from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
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


def parse_quiet_hours(value: str) -> tuple[int, int] | None:
    """"22-8" -> (22, 8); empty -> None. The window starts at the first hour and ends before the second."""
    value = value.strip()
    if not value:
        return None
    start, separator, end = value.partition("-")
    try:
        hours = (int(start), int(end))
    except ValueError:
        hours = None
    if not separator or hours is None or not all(0 <= hour <= 23 for hour in hours):
        raise ValueError(f"quiet hours must look like 22-8, got {value!r}")
    if hours[0] == hours[1]:
        raise ValueError(f"quiet hours must not start and end at the same hour, got {value!r}")
    return hours

def in_quiet_hours(hour: int, window: tuple[int, int] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_client_id: str = ""
    google_client_secret: str = ""
    gmail_refresh_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    gemini_max_rpm: int = 12
    gemini_timeout_seconds: float = Field(default=30, gt=0)
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
    # Tells the chat when failures pile up (JEV fallbacks, skipped mails, LLM errors, silent
    # listener): at least HEALTH_ALERT_MIN_EVENTS over the window. 0 minutes disables it.
    health_alert_window_minutes: int = Field(default=15, ge=0)
    health_alert_min_events: int = Field(default=3, ge=1)
    # Estimated LLM spend per local day above which the LLM is no longer called; 0 = no cap.
    llm_daily_budget_usd: float = Field(default=0, ge=0)
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
    # "shadow" asks JEV the attention questions and stores the answers, with no other effect;
    # "on" also puts forward the mails they say yes to.
    attention_mode: Literal["off", "shadow", "on"] = "off"
    attention_threshold: float = Field(default=0.5, ge=0, le=1)
    # Local hour from which the daily list of mails put forward is sent, silently; -1 disables.
    attention_list_hour: int = Field(default=-1, ge=-1, le=23)
    # IANA name, e.g. Europe/Paris; everything scheduled at a local hour reads it.
    timezone: str = "UTC"
    # Unsolicited messages (the daily list, follow-up offers) per local day, and local hours
    # during which none is sent, e.g. "22-8". Urgent alerts are never held back.
    proactive_daily_cap: int = Field(default=6, ge=1)
    quiet_hours: str = ""
    # "shadow" tracks the threads holding a mail I sent and who they wait for, with no message;
    # "on" also offers follow-ups.
    follow_up_mode: Literal["off", "shadow", "on"] = "off"
    # Weekdays without an answer before a follow-up is due.
    follow_up_after_days: int = Field(default=3, ge=1)
    follow_up_refresh_minutes: int = Field(default=15, ge=1)
    # Probability from which JEV's "does my mail wait for something" counts as yes.
    follow_up_threshold: float = Field(default=0.5, ge=0, le=1)
    # Local hour from which follow-ups are offered on weekdays, and at most how many a day.
    follow_up_hour: int = Field(default=10, ge=0, le=23)
    follow_up_daily_max: int = Field(default=3, ge=1)
    # Most recent sent threads followed; older ones beyond it are not.
    follow_up_max_threads: int = Field(default=50, ge=1)
    # Offers to unsubscribe from senders whose mail is always archived; needs the chat inbox.
    unsubscribe_proposals_enabled: bool = False
    unsubscribe_min_archived: int = Field(default=5, ge=2)

    # "on" answers free text written to the bot by reading the mailbox; it writes nothing.
    # Needs TELEGRAM_INBOUND_ENABLED and GEMINI_API_KEY.
    agent_mode: Literal["off", "on"] = "off"
    # Lets the agent show a reply to a mail, with [Envoyer] / [Annuler]. It sends nothing itself.
    agent_proposals_enabled: bool = False
    # Lets the agent keep, about a correspondent, passages of what the user wrote to it.
    agent_memory_enabled: bool = False
    # The model that drives the agent's tool loop; shares GEMINI_API_KEY, its rate limit and
    # its timeout with the analyzer.
    agent_model: str = "gemini-2.5-flash"
    # Model turns and tokens one run may use before it is stopped.
    agent_max_steps: int = Field(default=6, ge=1)
    agent_max_tokens: int = Field(default=60_000, ge=1)
    # What the agent may spend per local day, apart from LLM_DAILY_BUDGET_USD: an estimated
    # cost (0 = no cap; blind to a model without a known price) and a number of runs.
    agent_daily_budget_usd: float = Field(default=0, ge=0)
    agent_daily_max_runs: int = Field(default=100, ge=1)

    mail_provider: str = "gmail"
    classifier: str = "jev"
    llm_provider: str = "gemini"
    agent_provider: str = "gemini"
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

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone: {value!r}") from None
        return value

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet_hours(cls, value: str) -> str:
        parse_quiet_hours(value)
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @model_validator(mode="after")
    def _scheduled_hours_outside_quiet_hours(self) -> "Settings":
        # Both wait for their hour every day: inside the quiet window they would never go out.
        if self.attention_list_hour >= 0 and in_quiet_hours(
            self.attention_list_hour, self.quiet_hours_window
        ):
            raise ValueError(
                f"ATTENTION_LIST_HOUR={self.attention_list_hour} falls in QUIET_HOURS={self.quiet_hours}"
            )
        if self.follow_up_mode == "on" and in_quiet_hours(
            self.follow_up_hour, self.quiet_hours_window
        ):
            raise ValueError(
                f"FOLLOW_UP_HOUR={self.follow_up_hour} falls in QUIET_HOURS={self.quiet_hours}"
            )
        return self

    @property
    def quiet_hours_window(self) -> tuple[int, int] | None:
        return parse_quiet_hours(self.quiet_hours)

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
