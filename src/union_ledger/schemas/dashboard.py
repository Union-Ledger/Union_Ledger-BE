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

from union_ledger.models.enums import SettlementStatus
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
