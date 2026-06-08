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

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
    PresidentAuditorCard,
    PresidentDashboard,
    PresidentMemberCard,
    PresidentOrganizationInfo,
    PresidentTreasurerCard,
    TreasurerDashboard,
    TreasurerSettlementCard,
)
from union_ledger.services.dashboard import (
    NoAdminOrganization,
    PresidentAccessDenied,
    get_auditor_dashboard,
    get_president_dashboard,
    get_treasurer_dashboard,
)
from union_ledger.services.student_viewer import (
    format_period_label_short,
    settlement_status_label,
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
                audit_comment_count=r.audit_comment_count,
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


@router.get(
    "/president",
    response_model=PresidentDashboard,
    summary="회장 대시보드 (팀 현황/재정담당자 작업/감사위원 활동/구성원)",
)
async def president_dashboard(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    recent_limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> PresidentDashboard:
    try:
        summary = await get_president_dashboard(
            session,
            user_id=current_user.id,
            organization_id=organization_id,
            recent_limit=recent_limit,
        )
    except PresidentAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except NoAdminOrganization as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    org = summary.organization
    period = summary.current_period
    org_info = PresidentOrganizationInfo(
        id=org.id,
        name=org.name,
        college_name=org.college_name,
        department_name=org.department_name,
        president_name=summary.president_name,
        academic_year=period.academic_year if period else None,
        semester=period.semester if period else None,
        current_period_label=(
            format_period_label_short(period.academic_year, period.semester)
            if period
            else None
        ),
    )

    treasurer_work = [
        PresidentTreasurerCard(
            settlement_id=card.settlement.id,
            title=card.settlement.title,
            academic_year=card.settlement.academic_year,
            semester=card.settlement.semester,
            semester_label=format_period_label_short(
                card.settlement.academic_year, card.settlement.semester
            ),
            status=card.settlement.status,
            status_label=settlement_status_label(card.settlement),
            progress_percent=card.progress_percent,
            total_expense=card.total_expense,
            evidence_count=card.evidence_count,
            submitted_at=card.settlement.submitted_at,
            audited_at=card.settlement.audited_at,
            last_activity_at=card.last_activity_at,
        )
        for card in summary.treasurer_work
    ]

    auditor_activity = [
        PresidentAuditorCard(
            user_id=card.user.id,
            name=card.user.name,
            email=card.user.email,
            assigned_count=card.assigned_count,
            completed_count=card.completed_count,
            pending_count=card.pending_count,
            avg_review_days=card.avg_review_days,
            last_activity_at=card.last_activity_at,
        )
        for card in summary.auditor_activity
    ]

    members = [
        PresidentMemberCard(
            user_id=card.user.id,
            name=card.user.name,
            email=card.user.email,
            role=card.role,
            is_primary=card.is_primary,
        )
        for card in summary.members
    ]

    return PresidentDashboard(
        organization=org_info,
        team_member_count=summary.team_member_count,
        submitted_settlement_count=summary.submitted_settlement_count,
        audit_completed_count=summary.audit_completed_count,
        review_pending_count=summary.review_pending_count,
        treasurer_work=treasurer_work,
        auditor_activity=auditor_activity,
        members=members,
    )
