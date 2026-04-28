"""Dashboard endpoints (spec §5-1).

Routes:
  GET /dashboard/treasurer   — treasurer/admin role view
  GET /dashboard/auditor     — auditor role view

Both endpoints scope automatically to the caller's relevant memberships.
We do NOT 403 a treasurer asking for the auditor dashboard or vice versa
— they get an empty payload (zero counts, empty list). The FE can decide
whether to render a hint or just hide the section.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user
from union_ledger.db.session import get_db_session
from union_ledger.models.enums import MatchStatus
from union_ledger.schemas.audit_workflow import (
    AuditReconciliationSummary,
    AuditWorklistItem,
)
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.dashboard import (
    AuditorDashboard,
    TreasurerDashboard,
    TreasurerSettlementCard,
)
from union_ledger.services.dashboard import (
    get_auditor_dashboard,
    get_treasurer_dashboard,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/treasurer",
    response_model=TreasurerDashboard,
    summary="재정담당자 대시보드 (등록 증빙/총 지출/미매칭/진행율)",
)
async def treasurer_dashboard(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    recent_limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> TreasurerDashboard:
    summary = await get_treasurer_dashboard(
        session, user_id=current_user.id, recent_limit=recent_limit
    )
    return TreasurerDashboard(
        organization_count=summary.organization_count,
        settlement_counts_by_status=summary.settlement_counts_by_status,
        total_evidence_count=summary.total_evidence_count,
        total_evidence_amount=summary.total_evidence_amount,
        matched_count=summary.matched_count,
        unmatched_count=summary.unmatched_count,
        progress_percent=summary.progress_percent,
        recent_settlements=[
            TreasurerSettlementCard(
                settlement_id=r.settlement.id,
                organization_id=r.organization.id,
                organization_name=r.organization.name,
                title=r.settlement.title,
                academic_year=r.settlement.academic_year,
                semester=r.settlement.semester,
                status=r.settlement.status,
                submitted_at=r.settlement.submitted_at,
                audited_at=r.settlement.audited_at,
                evidence_count=r.evidence_count,
                bank_transaction_count=r.bank_transaction_count,
                matched_count=r.matched_count,
                unmatched_count=r.unmatched_count,
                total_evidence_amount=r.total_evidence_amount,
                progress_percent=r.progress_percent,
            )
            for r in summary.recent_settlements
        ],
    )


@router.get(
    "/auditor",
    response_model=AuditorDashboard,
    summary="감사위원 대시보드 (대기/진행중/완료 + 대기 결산안 미리보기)",
)
async def auditor_dashboard(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    pending_limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> AuditorDashboard:
    summary = await get_auditor_dashboard(
        session, user_id=current_user.id, pending_limit=pending_limit
    )
    return AuditorDashboard(
        organization_count=summary.organization_count,
        settlement_counts_by_status=summary.settlement_counts_by_status,
        pending_count=summary.pending_count,
        in_progress_count=summary.in_progress_count,
        completed_count=summary.completed_count,
        pending_settlements=[
            AuditWorklistItem(
                settlement_id=r.settlement.id,
                organization_id=r.organization.id,
                organization_name=r.organization.name,
                college_name=r.organization.college_name,
                department_name=r.organization.department_name,
                title=r.settlement.title,
                academic_year=r.settlement.academic_year,
                semester=r.settlement.semester,
                status=r.settlement.status,
                submitted_at=r.settlement.submitted_at,
                audited_at=r.settlement.audited_at,
                evidence_count=r.evidence_count,
                bank_transaction_count=r.bank_transaction_count,
                audit_comment_count=0,  # not tracked in dashboard rollup
                total_evidence_amount=r.total_evidence_amount,
                reconciliation=AuditReconciliationSummary(
                    matched=r.reconciliation_counts.get(MatchStatus.MATCHED, 0),
                    amount_mismatch=r.reconciliation_counts.get(
                        MatchStatus.AMOUNT_MISMATCH, 0
                    ),
                    date_mismatch=r.reconciliation_counts.get(
                        MatchStatus.DATE_MISMATCH, 0
                    ),
                    missing_bank_transaction=r.reconciliation_counts.get(
                        MatchStatus.MISSING_BANK_TRANSACTION, 0
                    ),
                    missing_evidence=r.reconciliation_counts.get(
                        MatchStatus.MISSING_EVIDENCE, 0
                    ),
                    manually_resolved=r.reconciliation_counts.get(
                        MatchStatus.MANUALLY_RESOLVED, 0
                    ),
                ),
            )
            for r in summary.pending_settlements
        ],
    )
