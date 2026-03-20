from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Union Ledger API"
    environment: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    database_url: str = (
        "postgresql+asyncpg://union_ledger:union_ledger@localhost:5432/union_ledger"
    )
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

