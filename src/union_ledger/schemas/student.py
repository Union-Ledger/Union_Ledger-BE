"""Student (일반 학우) viewer schemas — new endpoints only."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from union_ledger.models.enums import SettlementStatus


class StudentOrganizationSummary(BaseModel):
    # id is None when no student-council org exists yet for the student's
    # department (the dashboard still renders college/department + an empty
    # current period).
    id: uuid.UUID | None = None
    name: str
    college_name: str
    department_name: str


class StudentDashboardSummary(BaseModel):
    published_settlement_count: int
    current_period_total_amount: Decimal
    last_published_at: datetime | None
    total_view_count: int


class StudentProgressStep(BaseModel):
    key: str
    label: str
    state: str
    completed_at: datetime | None


class StudentCurrentPeriod(BaseModel):
    settlement_id: uuid.UUID | None
    academic_year: int
    semester: str
    label: str
    status: SettlementStatus | None
    status_label: str
    is_published: bool
    total_amount: Decimal
    evidence_count: int
    submitted_at: datetime | None
    audited_at: datetime | None
    published_at: datetime | None
    steps: list[StudentProgressStep]


class StudentRecentAuditResult(BaseModel):
    settlement_id: uuid.UUID
    academic_year: int
    semester: str
    label: str
    status: SettlementStatus
    status_label: str
    total_amount: Decimal
    audited_at: datetime | None
    published_at: datetime | None
    summary_comment: str | None


class StudentDashboard(BaseModel):
    organization: StudentOrganizationSummary
    summary: StudentDashboardSummary
    current_period: StudentCurrentPeriod
    """Same academic period as ``current_period`` — all student councils in the
    caller's college(s). Primary (my) organization is listed first."""
    college_period_overview: list[PeriodSettlementCard]
    recent_results: list[StudentRecentAuditResult]


class PublicPeriodOption(BaseModel):
    academic_year: int
    semester: str
    label: str
    settlement_count: int


class PeriodSettlementCard(BaseModel):
    id: uuid.UUID | None
    organization_id: uuid.UUID
    is_my_organization: bool = False
    organization_name: str
    college_name: str
    department_name: str
    academic_year: int
    semester: str
    label: str
    status: SettlementStatus | None
    status_label: str
    is_published: bool
    audited_at: datetime | None
    published_at: datetime | None
    total_amount: Decimal | None
    evidence_count: int | None
    view_count: int | None


class SettlementViewCountResponse(BaseModel):
    view_count: int
