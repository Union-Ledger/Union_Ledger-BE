from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from union_ledger.models.enums import SettlementStatus

# Allowed semester labels — keeps the FE dropdown and BE validation in sync.
ALLOWED_SEMESTERS = {"1", "2", "summer", "winter"}


def _normalize_semester(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in ALLOWED_SEMESTERS:
        raise ValueError(
            f"semester는 다음 중 하나여야 합니다: {sorted(ALLOWED_SEMESTERS)}"
        )
    return normalized


class SettlementCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    academic_year: int = Field(ge=2000, le=2100)
    semester: str = Field(min_length=1, max_length=20)
    template_id: uuid.UUID | None = None

    @field_validator("semester")
    @classmethod
    def _validate_semester(cls, value: str) -> str:
        return _normalize_semester(value)


class SettlementUpdateRequest(BaseModel):
    """All fields optional — only DRAFT settlements may be updated."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    academic_year: int | None = Field(default=None, ge=2000, le=2100)
    semester: str | None = Field(default=None, min_length=1, max_length=20)
    template_id: uuid.UUID | None = None

    @field_validator("semester")
    @classmethod
    def _validate_semester(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_semester(value)


class SettlementResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    template_id: uuid.UUID | None
    title: str
    academic_year: int
    semester: str
    status: SettlementStatus
    submitted_at: datetime | None
    audited_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
