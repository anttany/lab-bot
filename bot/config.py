from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LAB_SEATS = 25


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    admin_ids: str = ""
    group_chat_id: int | None = None
    db_path: str = "data/labbot.db"
    schedule_source: str = "file"
    schedule_path: str = "schedule.json"
    schedule_url: str = ""
    mtuci_login: str = ""
    mtuci_password: str = ""
    mtuci_group: str = ""
    mtuci_proxy: str = ""
    mtuci_months_ahead: int = 1
    timezone: str = "Europe/Moscow"
    signup_open_before_hours: int = 24
    notify_before_open_minutes: int = 15
    post_after_open_minutes: int = 15
    default_lab_seats: int = DEFAULT_LAB_SEATS

    @field_validator("group_chat_id", mode="before")
    @classmethod
    def empty_group_id(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def admins(self) -> set[int]:
        return {
            int(item.strip())
            for item in self.admin_ids.split(",")
            if item.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
