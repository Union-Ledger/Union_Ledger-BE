"""Dashboard schemas (spec §5-1).

Two role-specific dashboards:
  - Treasurer: 등록 증빙 건수, 총 지출액, 미매칭 건수, 감사 상태, 진행율
  - Auditor:   감사 대기/진행중/완료 결산안 목록 (counts + recent items)

Both return a single payload so the FE can paint the screen with one fetch.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from union_ledger.models.enums import RoleType, SettlementStatus
from union_ledger.schemas.audit_workflow import AuditWorklistItem

# --- Treasurer ----------------------------------------------------------


class TreasurerSettlementCard(BaseModel):
    """One line in the recent-settlements panel of the treasurer dashboard."""

    settlement_id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    title: str
    academic_year: int
    semester: str
    status: SettlementStatus
    submitted_at: datetime | None
    audited_at: datetime | None
    evidence_count: int
    bank_transaction_count: int
    matched_count: int
    unmatched_count: int
    total_evidence_amount: Decimal
    progress_percent: float


class TreasurerDashboard(BaseModel):
    organization_count: int
    settlement_counts_by_status: dict[SettlementStatus, int]
    total_evidence_count: int
    total_evidence_amount: Decimal
    matched_count: int
    unmatched_count: int
    progress_percent: float
    recent_settlements: list[TreasurerSettlementCard]


# --- Auditor ------------------------------------------------------------


class AuditorDashboard(BaseModel):
    organization_count: int
    settlement_counts_by_status: dict[SettlementStatus, int]
    pending_count: int  # SUBMITTED + RESUBMITTED — needs caller action
    in_progress_count: int  # UNDER_AUDIT (unused in current lifecycle)
    completed_count: int  # APPROVED + REJECTED
    pending_settlements: list[AuditWorklistItem]


# --- President (회장) ----------------------------------------------------


class PresidentOrganizationInfo(BaseModel):
    id: uuid.UUID
    name: str
    college_name: str
    department_name: str
    president_name: str | None
    academic_year: int | None
    semester: str | None
    current_period_label: str | None


class PresidentTreasurerCard(BaseModel):
    """One card in the "재정담당자 작업 현황" (Container) panel.

    Settlements are owned by the org, not a specific treasurer, so each card
    represents a settlement's progress rather than an individual treasurer.
    """

    settlement_id: uuid.UUID
    title: str
    academic_year: int
    semester: str
    semester_label: str
    status: SettlementStatus
    status_label: str
    progress_percent: float
    total_expense: Decimal
    evidence_count: int
    submitted_at: datetime | None
    audited_at: datetime | None
    last_activity_at: datetime | None


class PresidentAuditorCard(BaseModel):
    """One card in the "감사위원 활동 현황" panel (derived from audit comments)."""

    user_id: uuid.UUID
    name: str | None
    email: str
    assigned_count: int
    completed_count: int
    pending_count: int
    avg_review_days: float | None
    last_activity_at: datetime | None


class PresidentMemberCard(BaseModel):
    user_id: uuid.UUID
    name: str | None
    email: str
    role: RoleType
    is_primary: bool


class PresidentDashboard(BaseModel):
    organization: PresidentOrganizationInfo
    team_member_count: int
    submitted_settlement_count: int
    audit_completed_count: int
    review_pending_count: int
    treasurer_work: list[PresidentTreasurerCard]
    auditor_activity: list[PresidentAuditorCard]
    members: list[PresidentMemberCard]
