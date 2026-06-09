from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from union_ledger.models.enums import EvidenceStatus, EvidenceType, ExtractionMethod, PaymentMethod


class EvidenceResponse(BaseModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    organization_id: uuid.UUID
    evidence_type: EvidenceType
    status: EvidenceStatus
    extraction_method: ExtractionMethod | None
    source_file_name: str
    source_file_path: str
    extracted_payload: dict[str, Any]
    evidence_date: date | None
    merchant_name: str | None
    amount: Decimal | None
    payment_method: PaymentMethod | None
    budget_category: str | None
    is_refund: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EvidenceUpdateRequest(BaseModel):
    evidence_date: date | None = None
    merchant_name: str | None = None
    amount: Decimal | None = None
    payment_method: PaymentMethod | None = None
    budget_category: str | None = None
    is_refund: bool | None = None
    status: EvidenceStatus | None = None
    extracted_payload: dict[str, Any] | None = None


class OCRPreviewResponse(BaseModel):
    source_file_name: str
    evidence_type: EvidenceType
    status: EvidenceStatus
    extraction_method: ExtractionMethod
    extracted_payload: dict[str, Any]
    evidence_date: date | None
    merchant_name: str | None
    amount: Decimal | None
    payment_method: PaymentMethod | None
    is_refund: bool = False

