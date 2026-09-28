from pydantic_settings import BaseSettings, SettingsConfigDict


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
    fetch_max_age_days: int = 3
    fetch_query: str = ""
    sync_history: bool = False
    gmail_user_id: str = "me"
