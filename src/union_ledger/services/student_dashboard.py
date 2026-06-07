"""Student dashboard aggregation (``GET /dashboard/student``)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    Organization,
    OrganizationMembership,
    Settlement,
    User,
)
from union_ledger.services.public import is_settlement_public
from union_ledger.services.student_viewer import (
    PeriodSettlementRow,
    _period_sort_key,
    format_period_label_short,
    latest_audit_summary_comment,
    list_period_settlement_cards,
    resolve_org_by_user_identity,
    settlement_aggregates,
    sort_period_rows_primary_first,
)


class StudentDashboardError(Exception):
    pass


class OrganizationAccessDenied(StudentDashboardError):
    pass


class NoOrganizationMembership(StudentDashboardError):
    pass


@dataclass(slots=True)
class StudentDashboardData:
    organization: Organization
    published_settlement_count: int
    current_period_total_amount: Decimal
    last_published_at: object
    total_view_count: int
    current_period: Settlement | None
    current_period_amount: Decimal
    current_period_evidence_count: int
    recent_settlements: list[tuple[Settlement, Decimal, str | None]]
    college_period_overview: list[PeriodSettlementRow]


async def resolve_dashboard_organization(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None,
) -> Organization:
    if organization_id is not None:
        membership = await session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
            )
        )
        if membership is None:
            raise OrganizationAccessDenied("조직 접근 권한이 없습니다.")
        org = await session.get(Organization, organization_id)
        if org is None:
            raise OrganizationAccessDenied("조직을 찾을 수 없습니다.")
        return org

    membership = await session.scalar(
        select(OrganizationMembership)
        .where(OrganizationMembership.user_id == user_id)
        .order_by(
            OrganizationMembership.is_primary.desc(),
            OrganizationMembership.created_at.asc(),
        )
        .limit(1)
    )
    if membership is not None:
        org = await session.get(Organization, membership.organization_id)
        if org is not None:
            return org

    # Plain student (no membership): show their department's council if it
    # exists yet, otherwise a synthetic org carrying their college/department so
    # the dashboard still renders (empty current period, college overview).
    identity_org = await resolve_org_by_user_identity(session, user_id=user_id)
    if identity_org is not None:
        return identity_org

    user = await session.get(User, user_id)
    if user is None or not user.college_name or not user.department_name:
        raise NoOrganizationMembership("소속 정보가 없습니다.")
    return Organization(
        name=f"{user.college_name} {user.department_name}",
        college_name=user.college_name,
        department_name=user.department_name,
    )


def _pick_current_period_settlement(
    settlements: list[Settlement],
) -> Settlement | None:
    if not settlements:
        return None
    return max(
        settlements,
        key=lambda s: _period_sort_key(s.academic_year, s.semester),
    )


async def get_student_dashboard(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
) -> StudentDashboardData:
    org = await resolve_dashboard_organization(
        session, user_id=user_id, organization_id=organization_id
    )

    all_org_settlements = list(
        (
            await session.scalars(
                select(Settlement)
                .where(Settlement.organization_id == org.id)
                .order_by(Settlement.created_at.desc())
            )
        ).all()
    )

    published = [s for s in all_org_settlements if is_settlement_public(s)]
    published_ids = [s.id for s in published]
    published_aggregates = await settlement_aggregates(
        session, settlement_ids=published_ids
    )

    last_published_at = None
    if published:
        last_published_at = max(
            s.published_at for s in published if s.published_at is not None
        )

    total_view_count = sum(s.view_count for s in published)

    current_settlement = _pick_current_period_settlement(all_org_settlements)
    if current_settlement is not None:
        current_aggs = await settlement_aggregates(
            session, settlement_ids=[current_settlement.id]
        )
        current_amount, current_count = current_aggs.get(
            current_settlement.id, (Decimal(0), 0)
        )
    else:
        current_amount = Decimal(0)
        current_count = 0

    # KPI "이번 학기 지출" uses current period settlement totals when present.
    kpi_amount = current_amount if current_settlement is not None else Decimal(0)

    recent: list[tuple[Settlement, Decimal, str | None]] = []
    recent_candidates = sorted(
        published,
        key=lambda s: (
            s.published_at or s.audited_at or s.created_at,
        ),
        reverse=True,
    )[:5]
    for settlement in recent_candidates:
        amount, _ = published_aggregates.get(settlement.id, (Decimal(0), 0))
        comment = await latest_audit_summary_comment(
            session, settlement_id=settlement.id
        )
        recent.append((settlement, amount, comment))

    if current_settlement is not None:
        overview_year = current_settlement.academic_year
        overview_semester = current_settlement.semester
    else:
        overview_year = org.created_at.year if org.created_at else 2026
        overview_semester = "1"

    overview_rows = await list_period_settlement_cards(
        session,
        user_id=user_id,
        academic_year=overview_year,
        semester=overview_semester,
    )
    overview_rows = sort_period_rows_primary_first(
        overview_rows, primary_organization_id=org.id
    )

    return StudentDashboardData(
        organization=org,
        published_settlement_count=len(published),
        current_period_total_amount=kpi_amount,
        last_published_at=last_published_at,
        total_view_count=total_view_count,
        current_period=current_settlement,
        current_period_amount=current_amount,
        current_period_evidence_count=current_count,
        recent_settlements=recent,
        college_period_overview=overview_rows,
    )


def empty_current_period(org: Organization) -> dict[str, object]:
    """When the org has never created a settlement."""
    year = org.created_at.year if org.created_at else 2026
    semester = "1"
    return {
        "settlement_id": None,
        "academic_year": year,
        "semester": semester,
        "label": format_period_label_short(year, semester),
        "status": None,
        "status_label": "미제출",
        "is_published": False,
        "total_amount": Decimal(0),
        "evidence_count": 0,
        "submitted_at": None,
        "audited_at": None,
        "published_at": None,
        "steps": [
            {
                "key": "submit",
                "label": "결산 제출",
                "state": "pending",
                "completed_at": None,
            },
            {
                "key": "audit",
                "label": "감사 검토",
                "state": "pending",
                "completed_at": None,
            },
            {
                "key": "approve",
                "label": "승인 완료",
                "state": "pending",
                "completed_at": None,
            },
        ],
    }
