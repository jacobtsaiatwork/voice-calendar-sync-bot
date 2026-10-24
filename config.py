import os
from pydantic_settings import BaseSettings

DEFAULT_LINE_CHANNEL_SECRET = "dummy_channel_secret"
DEFAULT_LINE_CHANNEL_ACCESS_TOKEN = "dummy_channel_access_token"
DEFAULT_GEMINI_API_KEY = ""
DEFAULT_DATABASE_URL = "sqlite:///./voice_calendar_sync.db"

class Settings(BaseSettings):
    LINE_CHANNEL_SECRET: str = os.getenv("LINE_CHANNEL_SECRET", DEFAULT_LINE_CHANNEL_SECRET)
    LINE_CHANNEL_ACCESS_TOKEN: str = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", DEFAULT_LINE_CHANNEL_ACCESS_TOKEN)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", DEFAULT_GEMINI_API_KEY)
    DATABASE_URL: str = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    PORT: int = int(os.getenv("PORT", "8000"))
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "https://voice-calendar-sync-bot.vercel.app")

    # Google Calendar & Google Tasks 雙向同步設定
    GOOGLE_CALENDAR_ID: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    GOOGLE_TASKLIST_ID: str = os.getenv("GOOGLE_TASKLIST_ID", "@default")
    GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    GOOGLE_OAUTH_ACCESS_TOKEN: str = os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN", "")
    GOOGLE_APPS_SCRIPT_WEBHOOK_URL: str = os.getenv("GOOGLE_APPS_SCRIPT_WEBHOOK_URL", "")
    GOOGLE_CALENDAR_ICAL_URLS: str = os.getenv("GOOGLE_CALENDAR_ICAL_URLS", "")
    DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Asia/Taipei")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

def get_settings() -> Settings:
    sec = os.getenv("LINE_CHANNEL_SECRET", "") or DEFAULT_LINE_CHANNEL_SECRET
    tok = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "") or DEFAULT_LINE_CHANNEL_ACCESS_TOKEN
    gem = os.getenv("GEMINI_API_KEY", "") or DEFAULT_GEMINI_API_KEY
    db_url = os.getenv("DATABASE_URL", "") or DEFAULT_DATABASE_URL

    return Settings(
        LINE_CHANNEL_SECRET=sec,
        LINE_CHANNEL_ACCESS_TOKEN=tok,
        GEMINI_API_KEY=gem,
        DATABASE_URL=db_url
    )

settings = get_settings()
