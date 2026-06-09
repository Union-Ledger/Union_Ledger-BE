"""Student viewer services (periods list, per-period cards, view counts).

Does not modify legacy ``GET /public/settlements`` behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    AuditComment,
    Evidence,
    Organization,
    OrganizationMembership,
    Settlement,
    User,
    evidence_signed_amount,
)
from union_ledger.models.enums import SettlementStatus
from union_ledger.schemas.settlement import ALLOWED_SEMESTERS
from union_ledger.services.public import is_settlement_public

_SEMESTER_ORDER = {"1": 0, "2": 1, "summer": 2, "winter": 3}

_SEMESTER_LABEL_SHORT = {
    "1": "1학기",
    "2": "2학기",
    "summer": "여름학기",
    "winter": "겨울학기",
}

_SEMESTER_LABEL_LONG = {
    "1": "1학기",
    "2": "2학기",
    "summer": "여름학기",
    "winter": "겨울학기",
}


class StudentViewerError(Exception):
    pass


class SettlementViewNotAllowed(StudentViewerError):
    pass


class InvalidSemester(StudentViewerError):
    pass


@dataclass(slots=True)
class PeriodOptionRow:
    academic_year: int
    semester: str
    settlement_count: int


@dataclass(slots=True)
class PeriodSettlementRow:
    organization: Organization
    settlement: Settlement | None
    total_amount: Decimal | None
    evidence_count: int | None


def normalize_semester(semester: str) -> str:
    normalized = semester.strip().lower()
    if normalized not in ALLOWED_SEMESTERS:
        raise InvalidSemester(
            f"semester는 다음 중 하나여야 합니다: {sorted(ALLOWED_SEMESTERS)}"
        )
    return normalized


def format_period_label_short(academic_year: int, semester: str) -> str:
    return f"{academic_year}-{_SEMESTER_LABEL_SHORT.get(semester, semester)}"


def format_period_label_long(academic_year: int, semester: str) -> str:
    return f"{academic_year}학년도 {_SEMESTER_LABEL_LONG.get(semester, semester)}"


def settlement_status_label(settlement: Settlement | None) -> str:
    if settlement is None:
        return "미제출"
    status = settlement.status
    if status == SettlementStatus.DRAFT:
        return "작성 중"
    if status == SettlementStatus.READY_FOR_REVIEW:
        return "검토 준비"
    if status in (SettlementStatus.SUBMITTED, SettlementStatus.RESUBMITTED):
        return "감사 중"
    if status == SettlementStatus.REJECTED:
        return "반려"
    if status == SettlementStatus.APPROVED:
        if settlement.published_at is not None:
            return "공개 완료"
        return "승인 완료"
    return status.value


def build_progress_steps(settlement: Settlement) -> list[dict[str, object]]:
    if settlement.submitted_at is not None:
        submit_state = "completed"
    else:
        submit_state = "pending"

    if settlement.status in (
        SettlementStatus.SUBMITTED,
        SettlementStatus.RESUBMITTED,
    ):
        audit_state = "in_progress"
    elif settlement.audited_at is not None:
        audit_state = "completed"
    else:
        audit_state = "pending"

    if settlement.status == SettlementStatus.APPROVED:
        approve_state = "completed"
    else:
        approve_state = "pending"

    return [
        {
            "key": "submit",
            "label": "결산 제출",
            "state": submit_state,
            "completed_at": settlement.submitted_at,
        },
        {
            "key": "audit",
            "label": "감사 검토",
            "state": audit_state,
            "completed_at": settlement.audited_at
            if audit_state == "completed"
            else None,
        },
        {
            "key": "approve",
            "label": "승인 완료",
            "state": approve_state,
            "completed_at": settlement.audited_at
            if approve_state == "completed"
            else None,
        },
    ]


async def user_visible_colleges(
    session: AsyncSession, *, user_id: uuid.UUID
) -> set[str]:
    result = await session.scalars(
        select(Organization.college_name)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(OrganizationMembership.user_id == user_id)
        .distinct()
    )
    colleges = {name for name in result.all() if name}
    # A plain student holds no membership; fall back to the college recorded on
    # their account so they can still browse their college's published settlements.
    user = await session.get(User, user_id)
    if user is not None and user.college_name:
        colleges.add(user.college_name)
    return colleges


async def resolve_org_by_user_identity(
    session: AsyncSession, *, user_id: uuid.UUID
) -> Organization | None:
    """The 'my' org for a member-less student: the council whose college and
    department match the student's account. Returns None if none exists yet."""
    user = await session.get(User, user_id)
    if user is None or not user.college_name or not user.department_name:
        return None
    return await session.scalar(
        select(Organization)
        .where(
            Organization.college_name == user.college_name,
            Organization.department_name == user.department_name,
        )
        .order_by(Organization.created_at.asc())
        .limit(1)
    )


async def settlement_aggregates(
    session: AsyncSession, *, settlement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, int]]:
    if not settlement_ids:
        return {}
    rows = await session.execute(
        select(
            Evidence.settlement_id,
            func.coalesce(func.sum(evidence_signed_amount()), 0),
            func.count(Evidence.id),
        )
        .where(Evidence.settlement_id.in_(settlement_ids))
        .group_by(Evidence.settlement_id)
    )
    return {
        row[0]: (Decimal(row[1]), int(row[2])) for row in rows.all()
    }


def _period_sort_key(year: int, semester: str) -> tuple[int, int]:
    return (year, _SEMESTER_ORDER.get(semester, 99))


async def list_public_periods(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[PeriodOptionRow]:
    colleges = await user_visible_colleges(session, user_id=user_id)
    if not colleges:
        return []

    rows = await session.execute(
        select(
            Settlement.academic_year,
            Settlement.semester,
            func.count(Settlement.id),
        )
        .join(Organization, Organization.id == Settlement.organization_id)
        .where(
            Settlement.status == SettlementStatus.APPROVED,
            Settlement.published_at.is_not(None),
            Organization.college_name.in_(colleges),
        )
        .group_by(Settlement.academic_year, Settlement.semester)
    )
    options = [
        PeriodOptionRow(
            academic_year=int(row[0]),
            semester=str(row[1]),
            settlement_count=int(row[2]),
        )
        for row in rows.all()
    ]
    options.sort(
        key=lambda o: _period_sort_key(o.academic_year, o.semester),
        reverse=True,
    )
    return options


def sort_period_rows_primary_first(
    rows: list[PeriodSettlementRow],
    *,
    primary_organization_id: uuid.UUID | None,
) -> list[PeriodSettlementRow]:
    if primary_organization_id is None:
        return rows
    return sorted(
        rows,
        key=lambda r: (
            0 if r.organization.id == primary_organization_id else 1,
            r.organization.department_name,
        ),
    )


async def resolve_primary_organization_id(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    if organization_id is not None:
        return organization_id
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
        return membership.organization_id
    # No membership (plain student): treat their department's council as primary.
    org = await resolve_org_by_user_identity(session, user_id=user_id)
    return org.id if org else None


async def list_period_settlement_cards(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    academic_year: int,
    semester: str,
) -> list[PeriodSettlementRow]:
    semester = normalize_semester(semester)
    colleges = await user_visible_colleges(session, user_id=user_id)
    if not colleges:
        return []

    orgs = list(
        (
            await session.scalars(
                select(Organization)
                .where(Organization.college_name.in_(colleges))
                .order_by(Organization.department_name.asc())
            )
        ).all()
    )
    if not orgs:
        return []

    settlements = list(
        (
            await session.scalars(
                select(Settlement).where(
                    Settlement.organization_id.in_([o.id for o in orgs]),
                    Settlement.academic_year == academic_year,
                    Settlement.semester == semester,
                )
            )
        ).all()
    )
    settlement_by_org = {s.organization_id: s for s in settlements}
    settlement_ids = [s.id for s in settlements]
    aggregates = await settlement_aggregates(
        session, settlement_ids=settlement_ids
    )

    rows: list[PeriodSettlementRow] = []
    for org in orgs:
        settlement = settlement_by_org.get(org.id)
        if settlement is None:
            rows.append(
                PeriodSettlementRow(
                    organization=org,
                    settlement=None,
                    total_amount=None,
                    evidence_count=None,
                )
            )
            continue
        total, count = aggregates.get(settlement.id, (Decimal(0), 0))
        rows.append(
            PeriodSettlementRow(
                organization=org,
                settlement=settlement,
                total_amount=total,
                evidence_count=count,
            )
        )
    return rows


async def increment_settlement_view(
    session: AsyncSession,
    *,
    settlement: Settlement,
    user_id: uuid.UUID,
) -> int:
    if not is_settlement_public(settlement):
        raise SettlementViewNotAllowed("아직 공개되지 않은 결산안입니다.")

    org = await session.get(Organization, settlement.organization_id)
    if org is None:
        raise SettlementViewNotAllowed("조직 정보를 찾을 수 없습니다.")

    colleges = await user_visible_colleges(session, user_id=user_id)
    if org.college_name not in colleges:
        raise SettlementViewNotAllowed("열람 권한이 없는 결산안입니다.")

    settlement.view_count += 1
    await session.commit()
    await session.refresh(settlement)
    return settlement.view_count


async def latest_audit_summary_comment(
    session: AsyncSession, *, settlement_id: uuid.UUID
) -> str | None:
    comment = await session.scalar(
        select(AuditComment.comment)
        .where(AuditComment.settlement_id == settlement_id)
        .order_by(AuditComment.created_at.desc())
        .limit(1)
    )
    return comment
