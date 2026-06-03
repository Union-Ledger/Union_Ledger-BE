"""Student (일반 학우) endpoints — new routes only.

Does not alter existing ``public.py`` list/detail handlers.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user
from union_ledger.db.session import get_db_session
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.student import (
    PeriodSettlementCard,
    PublicPeriodOption,
    SettlementViewCountResponse,
    StudentCurrentPeriod,
    StudentDashboard,
    StudentDashboardSummary,
    StudentOrganizationSummary,
    StudentProgressStep,
    StudentRecentAuditResult,
)
from union_ledger.services.public import is_settlement_public
from union_ledger.services.settlement import SettlementNotFound, get_settlement_or_raise
from union_ledger.services.student_dashboard import (
    NoOrganizationMembership,
    OrganizationAccessDenied,
    empty_current_period,
    get_student_dashboard,
)
from union_ledger.services.student_viewer import (
    InvalidSemester,
    PeriodSettlementRow,
    SettlementViewNotAllowed,
    build_progress_steps,
    format_period_label_long,
    format_period_label_short,
    increment_settlement_view,
    list_period_settlement_cards,
    list_public_periods,
    resolve_primary_organization_id,
    settlement_status_label,
    sort_period_rows_primary_first,
)

router = APIRouter(tags=["student"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _rows_to_period_cards(
    rows: list[PeriodSettlementRow],
    *,
    academic_year: int,
    semester: str,
    primary_organization_id: uuid.UUID | None,
) -> list[PeriodSettlementCard]:
    semester_norm = semester.strip().lower()
    label = format_period_label_short(academic_year, semester_norm)
    cards: list[PeriodSettlementCard] = []
    for row in rows:
        settlement = row.settlement
        is_published = (
            settlement is not None and is_settlement_public(settlement)
        )
        cards.append(
            PeriodSettlementCard(
                id=settlement.id if settlement else None,
                organization_id=row.organization.id,
                is_my_organization=(
                    primary_organization_id is not None
                    and row.organization.id == primary_organization_id
                ),
                organization_name=row.organization.name,
                college_name=row.organization.college_name,
                department_name=row.organization.department_name,
                academic_year=academic_year,
                semester=semester_norm,
                label=label,
                status=settlement.status if settlement else None,
                status_label=settlement_status_label(settlement),
                is_published=is_published,
                audited_at=settlement.audited_at if settlement else None,
                published_at=settlement.published_at if settlement else None,
                total_amount=row.total_amount if settlement else None,
                evidence_count=row.evidence_count if settlement else None,
                view_count=settlement.view_count
                if settlement and is_published
                else None,
            )
        )
    return cards


def _build_current_period(data) -> StudentCurrentPeriod:
    settlement = data.current_period
    org = data.organization
    if settlement is None:
        raw = empty_current_period(org)
        return StudentCurrentPeriod(
            settlement_id=None,
            academic_year=int(raw["academic_year"]),  # type: ignore[arg-type]
            semester=str(raw["semester"]),
            label=str(raw["label"]),
            status=None,
            status_label=str(raw["status_label"]),
            is_published=False,
            total_amount=data.current_period_amount,
            evidence_count=data.current_period_evidence_count,
            submitted_at=None,
            audited_at=None,
            published_at=None,
            steps=[StudentProgressStep.model_validate(s) for s in raw["steps"]],  # type: ignore[arg-type]
        )

    steps = build_progress_steps(settlement)
    return StudentCurrentPeriod(
        settlement_id=settlement.id,
        academic_year=settlement.academic_year,
        semester=settlement.semester,
        label=format_period_label_short(settlement.academic_year, settlement.semester),
        status=settlement.status,
        status_label=settlement_status_label(settlement),
        is_published=is_settlement_public(settlement),
        total_amount=data.current_period_amount,
        evidence_count=data.current_period_evidence_count,
        submitted_at=settlement.submitted_at,
        audited_at=settlement.audited_at,
        published_at=settlement.published_at,
        steps=[StudentProgressStep.model_validate(s) for s in steps],
    )


@router.get(
    "/dashboard/student",
    response_model=StudentDashboard,
    summary="학생 대시보드 (소속 학생회·현재 학기 진행·공개 결산 요약)",
)
async def student_dashboard(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    organization_id: Annotated[
        uuid.UUID | None,
        Query(description="미지정 시 primary 멤버십 조직"),
    ] = None,
) -> StudentDashboard:
    try:
        data = await get_student_dashboard(
            session,
            user_id=current_user.id,
            organization_id=organization_id,
        )
    except OrganizationAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except NoOrganizationMembership as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    recent_results = [
        StudentRecentAuditResult(
            settlement_id=s.id,
            academic_year=s.academic_year,
            semester=s.semester,
            label=format_period_label_short(s.academic_year, s.semester),
            status=s.status,
            status_label=settlement_status_label(s),
            total_amount=amount,
            audited_at=s.audited_at,
            published_at=s.published_at,
            summary_comment=comment,
        )
        for s, amount, comment in data.recent_settlements
    ]

    current_period = _build_current_period(data)

    return StudentDashboard(
        organization=StudentOrganizationSummary(
            id=data.organization.id,
            name=data.organization.name,
            college_name=data.organization.college_name,
            department_name=data.organization.department_name,
        ),
        summary=StudentDashboardSummary(
            published_settlement_count=data.published_settlement_count,
            current_period_total_amount=data.current_period_total_amount,
            last_published_at=data.last_published_at,  # type: ignore[arg-type]
            total_view_count=data.total_view_count,
        ),
        current_period=current_period,
        college_period_overview=_rows_to_period_cards(
            data.college_period_overview,
            academic_year=current_period.academic_year,
            semester=current_period.semester,
            primary_organization_id=data.organization.id,
        ),
        recent_results=recent_results,
    )


@router.get(
    "/public/periods",
    response_model=list[PublicPeriodOption],
    summary="공개 결산 학기 목록 (드롭다운)",
)
async def public_periods(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[PublicPeriodOption]:
    rows = await list_public_periods(session, user_id=current_user.id)
    return [
        PublicPeriodOption(
            academic_year=r.academic_year,
            semester=r.semester,
            label=format_period_label_long(r.academic_year, r.semester),
            settlement_count=r.settlement_count,
        )
        for r in rows
    ]


@router.get(
    "/public/periods/{academic_year}/{semester}/settlements",
    response_model=list[PeriodSettlementCard],
    summary="학기별 학과 결산 카드 (진행 상태 포함)",
)
async def public_period_settlements(
    academic_year: int,
    semester: str,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[PeriodSettlementCard]:
    try:
        rows = await list_period_settlement_cards(
            session,
            user_id=current_user.id,
            academic_year=academic_year,
            semester=semester,
        )
    except InvalidSemester as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    primary_org_id = await resolve_primary_organization_id(
        session, user_id=current_user.id
    )
    rows = sort_period_rows_primary_first(
        rows, primary_organization_id=primary_org_id
    )
    semester_norm = semester.strip().lower()
    return _rows_to_period_cards(
        rows,
        academic_year=academic_year,
        semester=semester_norm,
        primary_organization_id=primary_org_id,
    )


@router.post(
    "/public/settlements/{settlement_id}/views",
    response_model=SettlementViewCountResponse,
    summary="공개 결산안 조회수 증가",
)
async def record_settlement_view(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> SettlementViewCountResponse:
    try:
        settlement = await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        count = await increment_settlement_view(
            session,
            settlement=settlement,
            user_id=current_user.id,
        )
    except SettlementViewNotAllowed as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return SettlementViewCountResponse(view_count=count)
