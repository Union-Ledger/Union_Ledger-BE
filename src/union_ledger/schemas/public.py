"""Public Viewer schemas (spec §4).

The 일반 학우 view exposes only settlements with `status=APPROVED` AND
`published_at IS NOT NULL`. Each user is scoped to settlements of any
college they have a membership in (their signup-org college plus any orgs
they were invited into).

Sensitive fields the audit-side schemas carry (e.g. `source_file_path`,
`extracted_payload`) are intentionally omitted here to avoid leaking PII /
internal structure to non-staff readers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from union_ledger.models.enums import (
    ArtifactStatus,
    ArtifactType,
    EvidenceType,
    PaymentMethod,
)


class PublicSettlementSummary(BaseModel):
    """One row of `GET /public/settlements`."""

    id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    college_name: str
    department_name: str
    title: str
    academic_year: int
    semester: str
    published_at: datetime
    total_amount: Decimal
    item_count: int


class PublicArtifactSummary(BaseModel):
    """Subset of artifact info safe for public viewers — no `file_path`."""

    id: uuid.UUID
    artifact_type: ArtifactType
    status: ArtifactStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicSettlementDetail(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    college_name: str
    department_name: str
    title: str
    academic_year: int
    semester: str
    submitted_at: datetime | None
    audited_at: datetime | None
    published_at: datetime
    total_amount: Decimal
    item_count: int
    artifacts: list[PublicArtifactSummary]


class PublicSettlementItem(BaseModel):
    """One expense line on the public itemized view."""

    evidence_id: uuid.UUID
    evidence_type: EvidenceType
    evidence_date: date | None
    merchant_name: str | None
    amount: Decimal | None
    payment_method: PaymentMethod | None
    budget_category: str | None
    # 구분 — event/purpose group; what the public breakdown displays.
    group_name: str | None
    has_evidence_file: bool


class PublicEvidenceDetail(BaseModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    evidence_type: EvidenceType
    evidence_date: date | None
    merchant_name: str | None
    amount: Decimal | None
    payment_method: PaymentMethod | None
    budget_category: str | None
    group_name: str | None
    source_file_name: str
    has_evidence_file: bool
