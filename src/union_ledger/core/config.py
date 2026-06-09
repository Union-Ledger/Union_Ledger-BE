from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Local CPU OCR speed knobs. Each preprocessing variant is a full OCR pass,
    # so the original 6 variants made CPU OCR ~6× slower. Cap the passes:
    #   0 = no cap (max accuracy, slowest), 3 = balanced default (3-way voting),
    #   1 = fastest (single best variant). Set via OCR_MAX_VARIANTS.
    # mkldnn = oneDNN CPU acceleration (safe speedup on most x86 CPUs).
    ocr_max_variants: int = 3
    ocr_enable_mkldnn: bool = True
    # Receipt extraction provider: "local" = PaddleOCR (default, free, offline),
    # "clova" = Naver CLOVA Receipt OCR (Korean-specialized, returns structured
    # fields). When "clova" is set, configure the invoke URL + secret from the
    # NCP console; on any CLOVA error we fall back to local OCR unless disabled.
    ocr_provider: str = "local"
    clova_ocr_invoke_url: str = ""
    clova_ocr_secret_key: str = ""
    clova_ocr_timeout_seconds: int = 30
    clova_ocr_fallback_to_local: bool = True
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
    # Platform operators (the maintaining developers). Only these accounts may
    # review 회장(admin) applications and create organizations directly. Set via
    # the OPERATOR_EMAILS env var as a comma-separated list.
    operator_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="OPERATOR_EMAILS",
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

    @field_validator("operator_emails", mode="before")
    @classmethod
    def parse_operator_emails(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return value

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
