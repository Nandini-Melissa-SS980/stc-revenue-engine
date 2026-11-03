"""Runtime settings (infrastructure only). Business thresholds live in workspace_settings."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RE_", extra="ignore")

    env: Literal["local", "test", "staging", "production"] = "local"
    database_url: str = "postgresql+asyncpg://revenue_app:revenue_app@localhost:5432/revenue"
    redis_url: str = "redis://localhost:6379/0"
    ai_service_url: str = "http://localhost:8100"


@lru_cache
def get_settings() -> Settings:
    return Settings()
