from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Union Ledger API"
    environment: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    database_url: str = (
        "postgresql+asyncpg://union_ledger:union_ledger@localhost:5432/union_ledger"
    )
    jwt_secret_key: str = Field(
        default="change-me-in-env",
        validation_alias="JWT_SECRET_KEY",
    )
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    email_verification_code_expire_minutes: int = 5
    email_verification_verified_expire_minutes: int = 30
    smtp_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_from_name: str = "Union Ledger"
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: int = 15
    storage_root: Path = Path("storage")
    ocr_engine: str = "paddleocr"
    ocr_mode: str = "local"  # "local" = CPU PaddleOCR, "modal" = Modal GPU Worker
    paddleocr_lang: str = "korean"
    ocr_min_line_confidence: float = 0.25
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug(cls, value: object) -> object:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "development", "dev"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "production", "prod"}:
                return False
        return value

    @field_validator("storage_root", mode="before")
    @classmethod
    def parse_storage_root(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value)
        return Path("storage")

    @field_validator("smtp_use_ssl")
    @classmethod
    def validate_smtp_ssl_with_tls(cls, value: bool, info):
        smtp_use_tls = bool(info.data.get("smtp_use_tls", True))
        if value and smtp_use_tls:
            raise ValueError("SMTP_USE_SSL and SMTP_USE_TLS cannot both be true")
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
