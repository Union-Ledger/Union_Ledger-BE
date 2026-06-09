"""Settings validation — production must reject default/weak JWT secrets.

The guard only fires when ENVIRONMENT=production, so local/test runs (the
default 'local') keep working with the placeholder secret. Driven via env
vars (monkeypatch) because jwt_secret_key uses a validation_alias, so env is
the realistic configuration path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from union_ledger.core.config import Settings


def test_production_rejects_default_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "change-me-in-env")
    with pytest.raises(ValidationError):
        Settings()


def test_production_rejects_example_placeholder_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "change-me-long-random-secret")
    with pytest.raises(ValidationError):
        Settings()


def test_production_rejects_short_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    with pytest.raises(ValidationError):
        Settings()


def test_production_accepts_strong_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "a-sufficiently-long-random-secret-value-123"
    )
    settings = Settings()
    assert settings.environment == "production"


def test_local_allows_default_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local/dev keeps working with the placeholder — guard is production-only.
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("JWT_SECRET_KEY", "change-me-in-env")
    settings = Settings()
    assert settings.jwt_secret_key == "change-me-in-env"
