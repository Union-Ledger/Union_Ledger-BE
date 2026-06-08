"""Dashboard services (spec §5-1).

Each role's dashboard is a single read API: roll up counts and pick a
"recent" list. We aggregate via group-by queries rather than walking
relationships from each settlement so the cost is bounded by the number
of distinct (settlement, status) pairs, not by per-row joins.

`progress_percent` is a coarse signal derived from reconciliation status:
  matched + manually_resolved over total reconciliation rows.
If there are no reconciliation rows yet it's reported as 0.0; the FE can
choose to render that as "auto-match not run yet".
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    AuditComment,
    BankStatementUpload,
    BankTransaction,
    Evidence,
    Organization,
    OrganizationMembership,
    ReconciliationResult,
    Settlement,
    User,
)
from union_ledger.models.enums import MatchStatus, RoleType, SettlementStatus

# Roles that get the treasurer dashboard. Admins manage the org and need
# the same visibility, so we lump them in.
_TREASURER_DASHBOARD_ROLES = {RoleType.TREASURER, RoleType.PRESIDENT}

# Reconciliation statuses that count as "matched" for progress %.
_MATCHED_LIKE = {MatchStatus.MATCHED, MatchStatus.MANUALLY_RESOLVED}


@dataclass(slots=True)
class _SettlementRollup:
    settlement: Settlement
    organization: Organization
    evidence_count: int = 0
    bank_transaction_count: int = 0
    audit_comment_count: int = 0
    total_evidence_amount: Decimal = Decimal(0)
    reconciliation_counts: dict[MatchStatus, int] = field(default_factory=dict)

    @property
    def matched_count(self) -> int:
        return sum(
            self.reconciliation_counts.get(s, 0) for s in _MATCHED_LIKE
        )

    @property
    def unmatched_count(self) -> int:
        total = sum(self.reconciliation_counts.values())
        return total - self.matched_count

    @property
    def progress_percent(self) -> float:
        total = sum(self.reconciliation_counts.values())
        if total == 0:
            return 0.0
        return round(self.matched_count / total * 100, 2)


@dataclass(slots=True)
class TreasurerSummary:
    organization_count: int
    settlement_counts_by_status: dict[SettlementStatus, int]
    total_evidence_count: int
    total_evidence_amount: Decimal
    matched_count: int
    unmatched_count: int
    progress_percent: float
    recent_settlements: Sequence[_SettlementRollup]


@dataclass(slots=True)
class AuditorSummary:
    organization_count: int
    settlement_counts_by_status: dict[SettlementStatus, int]
    pending_count: int
    in_progress_count: int
    completed_count: int
    pending_settlements: Sequence[_SettlementRollup]


# --- Shared helpers ------------------------------------------------------


async def _user_org_ids(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    roles: Sequence[RoleType],
) -> list[uuid.UUID]:
    if not roles:
        return []
    result = await session.scalars(
        select(OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role.in_(set(roles)),
        )
        .distinct()
    )
    return list(result.all())


async def _rollups_for_settlements(
    session: AsyncSession,
    *,
    settlements: Sequence[Settlement],
    orgs_by_id: dict[uuid.UUID, Organization],
) -> list[_SettlementRollup]:
    if not settlements:
        return []
    settlement_ids = [s.id for s in settlements]

    evidence_rows = await session.execute(
        select(
            Evidence.settlement_id,
            func.count(Evidence.id),
            func.coalesce(func.sum(func.abs(Evidence.amount)), 0),
        )
        .where(Evidence.settlement_id.in_(settlement_ids))
        .group_by(Evidence.settlement_id)
    )
    evidence_summary: dict[uuid.UUID, tuple[int, Decimal]] = {
        row[0]: (int(row[1]), Decimal(row[2])) for row in evidence_rows.all()
    }

    tx_rows = await session.execute(
        select(BankStatementUpload.settlement_id, func.count(BankTransaction.id))
        .join(BankTransaction, BankTransaction.upload_id == BankStatementUpload.id)
        .where(BankStatementUpload.settlement_id.in_(settlement_ids))
        .group_by(BankStatementUpload.settlement_id)
    )
    tx_count_by_settlement: dict[uuid.UUID, int] = {
        row[0]: int(row[1]) for row in tx_rows.all()
    }

    comment_rows = await session.execute(
        select(AuditComment.settlement_id, func.count(AuditComment.id))
        .where(AuditComment.settlement_id.in_(settlement_ids))
        .group_by(AuditComment.settlement_id)
    )
    comment_count_by_settlement: dict[uuid.UUID, int] = {
        row[0]: int(row[1]) for row in comment_rows.all()
    }

    rec_rows = await session.execute(
        select(
            ReconciliationResult.settlement_id,
            ReconciliationResult.status,
            func.count(ReconciliationResult.id),
        )
        .where(ReconciliationResult.settlement_id.in_(settlement_ids))
        .group_by(
            ReconciliationResult.settlement_id,
            ReconciliationResult.status,
        )
    )
    rec_summary: dict[uuid.UUID, dict[MatchStatus, int]] = {}
    for sid, match_status, n in rec_rows.all():
        rec_summary.setdefault(sid, {})[match_status] = int(n)

    rollups: list[_SettlementRollup] = []
    for settlement in settlements:
        ev_count, ev_total = evidence_summary.get(
            settlement.id, (0, Decimal(0))
        )
        rollups.append(
            _SettlementRollup(
                settlement=settlement,
                organization=orgs_by_id[settlement.organization_id],
                evidence_count=ev_count,
                bank_transaction_count=tx_count_by_settlement.get(
                    settlement.id, 0
                ),
                audit_comment_count=comment_count_by_settlement.get(
                    settlement.id, 0
                ),
                total_evidence_amount=ev_total,
                reconciliation_counts=rec_summary.get(settlement.id, {}),
            )
        )
    return rollups


async def _orgs_by_id(
    session: AsyncSession, org_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Organization]:
    if not org_ids:
        return {}
    result = await session.scalars(
        select(Organization).where(Organization.id.in_(org_ids))
    )
    return {org.id: org for org in result.all()}


# --- Treasurer -----------------------------------------------------------


async def get_treasurer_dashboard(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    recent_limit: int = 10,
) -> TreasurerSummary:
    org_ids = await _user_org_ids(
        session, user_id=user_id, roles=tuple(_TREASURER_DASHBOARD_ROLES)
    )
    if not org_ids:
        return TreasurerSummary(
            organization_count=0,
            settlement_counts_by_status={},
            total_evidence_count=0,
            total_evidence_amount=Decimal(0),
            matched_count=0,
            unmatched_count=0,
            progress_percent=0.0,
            recent_settlements=[],
        )

    settlement_count_rows = await session.execute(
        select(Settlement.status, func.count(Settlement.id))
        .where(Settlement.organization_id.in_(org_ids))
        .group_by(Settlement.status)
    )
    counts_by_status: dict[SettlementStatus, int] = {
        row[0]: int(row[1]) for row in settlement_count_rows.all()
    }

    settlements_result = await session.scalars(
        select(Settlement)
        .where(Settlement.organization_id.in_(org_ids))
        .order_by(
            Settlement.submitted_at.desc().nullslast(),
            Settlement.created_at.desc(),
        )
        .limit(recent_limit)
    )
    recent = list(settlements_result.all())

    orgs_by_id = await _orgs_by_id(session, org_ids)

    # Aggregate evidence + reconciliation across ALL settlements, not just
    # the recent slice — the "총 지출액" / 진행율 are org-wide numbers.
    all_settlements_result = await session.scalars(
        select(Settlement.id).where(Settlement.organization_id.in_(org_ids))
    )
    all_settlement_ids = list(all_settlements_result.all())

    total_evidence_count = 0
    total_evidence_amount = Decimal(0)
    if all_settlement_ids:
        ev_row = (
            await session.execute(
                select(
                    func.count(Evidence.id),
                    func.coalesce(func.sum(func.abs(Evidence.amount)), 0),
                ).where(Evidence.settlement_id.in_(all_settlement_ids))
            )
        ).one()
        total_evidence_count = int(ev_row[0])
        total_evidence_amount = Decimal(ev_row[1])

    matched = 0
    total_recon = 0
    if all_settlement_ids:
        rec_rows = await session.execute(
            select(ReconciliationResult.status, func.count(ReconciliationResult.id))
            .where(ReconciliationResult.settlement_id.in_(all_settlement_ids))
            .group_by(ReconciliationResult.status)
        )
        for s, n in rec_rows.all():
            total_recon += int(n)
            if s in _MATCHED_LIKE:
                matched += int(n)
    unmatched = total_recon - matched
    progress = round(matched / total_recon * 100, 2) if total_recon else 0.0

    rollups = await _rollups_for_settlements(
        session, settlements=recent, orgs_by_id=orgs_by_id
    )

    return TreasurerSummary(
        organization_count=len(org_ids),
        settlement_counts_by_status=counts_by_status,
        total_evidence_count=total_evidence_count,
        total_evidence_amount=total_evidence_amount,
        matched_count=matched,
        unmatched_count=unmatched,
        progress_percent=progress,
        recent_settlements=rollups,
    )


# --- Auditor -------------------------------------------------------------


_PENDING_STATUSES = frozenset({SettlementStatus.SUBMITTED, SettlementStatus.RESUBMITTED})
_IN_PROGRESS_STATUSES = frozenset({SettlementStatus.UNDER_AUDIT})
_COMPLETED_STATUSES = frozenset({SettlementStatus.APPROVED, SettlementStatus.REJECTED})


async def get_auditor_dashboard(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    pending_limit: int = 10,
) -> AuditorSummary:
    org_ids = await _user_org_ids(
        session, user_id=user_id, roles=(RoleType.AUDITOR,)
    )
    if not org_ids:
        return AuditorSummary(
            organization_count=0,
            settlement_counts_by_status={},
            pending_count=0,
            in_progress_count=0,
            completed_count=0,
            pending_settlements=[],
        )

    settlement_count_rows = await session.execute(
        select(Settlement.status, func.count(Settlement.id))
        .where(Settlement.organization_id.in_(org_ids))
        .group_by(Settlement.status)
    )
    counts_by_status: dict[SettlementStatus, int] = {
        row[0]: int(row[1]) for row in settlement_count_rows.all()
    }
    pending = sum(counts_by_status.get(s, 0) for s in _PENDING_STATUSES)
    in_progress = sum(counts_by_status.get(s, 0) for s in _IN_PROGRESS_STATUSES)
    completed = sum(counts_by_status.get(s, 0) for s in _COMPLETED_STATUSES)

    pending_settlements_result = await session.scalars(
        select(Settlement)
        .where(
            Settlement.organization_id.in_(org_ids),
            Settlement.status.in_(_PENDING_STATUSES),
        )
        .order_by(
            Settlement.submitted_at.desc().nullslast(),
            Settlement.created_at.desc(),
        )
        .limit(pending_limit)
    )
    pending_recent = list(pending_settlements_result.all())

    orgs_by_id = await _orgs_by_id(session, org_ids)
    rollups = await _rollups_for_settlements(
        session, settlements=pending_recent, orgs_by_id=orgs_by_id
    )

    return AuditorSummary(
        organization_count=len(org_ids),
        settlement_counts_by_status=counts_by_status,
        pending_count=pending,
        in_progress_count=in_progress,
        completed_count=completed,
        pending_settlements=rollups,
    )


# --- President (회장) -----------------------------------------------------
#
# The 회장 대시보드 is org-scoped: it rolls up the whole team's status for the
# ADMIN who runs the org. Unlike the treasurer/auditor dashboards (which span
# every org a user belongs to), this one is anchored to a single organization.
#
# Data-model note: settlements are owned by the *organization*, not by an
# individual treasurer, and audit decisions don't record which auditor made
# them. So the "재정담당자 작업 현황" cards are per-settlement progress cards,
# and auditor activity is derived from the audit comments each auditor authored
# (the only per-auditor signal we have).

_SUBMITTED_STATUSES = frozenset(
    {
        SettlementStatus.SUBMITTED,
        SettlementStatus.UNDER_AUDIT,
        SettlementStatus.RESUBMITTED,
        SettlementStatus.APPROVED,
        SettlementStatus.REJECTED,
    }
)
_AUDIT_COMPLETED_STATUSES = frozenset(
    {SettlementStatus.APPROVED, SettlementStatus.REJECTED}
)
_REVIEW_PENDING_STATUSES = frozenset(
    {
        SettlementStatus.SUBMITTED,
        SettlementStatus.RESUBMITTED,
        SettlementStatus.UNDER_AUDIT,
    }
)


class PresidentDashboardError(Exception):
    pass


class PresidentAccessDenied(PresidentDashboardError):
    pass


class NoAdminOrganization(PresidentDashboardError):
    pass


@dataclass(slots=True)
class TreasurerWorkCard:
    settlement: Settlement
    evidence_count: int
    total_expense: Decimal
    progress_percent: float
    last_activity_at: datetime | None


@dataclass(slots=True)
class AuditorActivityCard:
    user: User
    assigned_count: int
    completed_count: int
    pending_count: int
    avg_review_days: float | None
    last_activity_at: datetime | None


@dataclass(slots=True)
class MemberCard:
    user: User
    role: RoleType
    is_primary: bool


@dataclass(slots=True)
class PresidentSummary:
    organization: Organization
    president_name: str | None
    current_period: Settlement | None
    team_member_count: int
    submitted_settlement_count: int
    audit_completed_count: int
    review_pending_count: int
    treasurer_work: list[TreasurerWorkCard]
    auditor_activity: list[AuditorActivityCard]
    members: list[MemberCard]


async def resolve_admin_organization(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None,
) -> Organization:
    """Resolve the org for the president dashboard, enforcing ADMIN access.

    With ``organization_id`` the caller must be ADMIN of that org. Without it
    we pick their primary (or earliest) ADMIN membership.
    """
    if organization_id is not None:
        membership = await session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.role == RoleType.ADMIN,
            )
        )
        if membership is None:
            raise PresidentAccessDenied("회장(Admin) 권한이 있는 조직이 아닙니다.")
        org = await session.get(Organization, organization_id)
        if org is None:
            raise PresidentAccessDenied("조직을 찾을 수 없습니다.")
        return org

    membership = await session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role == RoleType.ADMIN,
        )
        .order_by(
            OrganizationMembership.is_primary.desc(),
            OrganizationMembership.created_at.asc(),
        )
        .limit(1)
    )
    if membership is None:
        raise NoAdminOrganization("회장(Admin) 권한이 있는 조직이 없습니다.")
    org = await session.get(Organization, membership.organization_id)
    if org is None:
        raise NoAdminOrganization("조직을 찾을 수 없습니다.")
    return org


def _settlement_last_activity(settlement: Settlement) -> datetime | None:
    candidates = [
        settlement.updated_at,
        settlement.submitted_at,
        settlement.audited_at,
        settlement.published_at,
    ]
    present = [ts for ts in candidates if ts is not None]
    return max(present) if present else None


async def get_president_dashboard(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
    recent_limit: int = 10,
) -> PresidentSummary:
    org = await resolve_admin_organization(
        session, user_id=user_id, organization_id=organization_id
    )

    memberships = (
        await session.scalars(
            select(OrganizationMembership)
            .where(OrganizationMembership.organization_id == org.id)
            .order_by(OrganizationMembership.created_at.asc())
        )
    ).all()

    member_user_ids = {m.user_id for m in memberships}
    users_by_id: dict[uuid.UUID, User] = {}
    if member_user_ids:
        users_result = await session.scalars(
            select(User).where(User.id.in_(member_user_ids))
        )
        users_by_id = {u.id: u for u in users_result.all()}

    president_name: str | None = None
    primary_admin = next(
        (m for m in memberships if m.role == RoleType.ADMIN and m.is_primary),
        next((m for m in memberships if m.role == RoleType.ADMIN), None),
    )
    if primary_admin is not None:
        president_user = users_by_id.get(primary_admin.user_id)
        president_name = president_user.name if president_user else None

    # Build the member roster (dedupe by user+role pairs, keep order).
    members: list[MemberCard] = []
    for m in memberships:
        user = users_by_id.get(m.user_id)
        if user is None:
            continue
        members.append(MemberCard(user=user, role=m.role, is_primary=m.is_primary))

    # All settlements for this org (status counts + cards both need them).
    settlements = list(
        (
            await session.scalars(
                select(Settlement)
                .where(Settlement.organization_id == org.id)
                .order_by(
                    Settlement.submitted_at.desc().nullslast(),
                    Settlement.created_at.desc(),
                )
            )
        ).all()
    )

    submitted_count = sum(
        1 for s in settlements if s.status in _SUBMITTED_STATUSES
    )
    audit_completed_count = sum(
        1 for s in settlements if s.status in _AUDIT_COMPLETED_STATUSES
    )
    review_pending_count = sum(
        1 for s in settlements if s.status in _REVIEW_PENDING_STATUSES
    )

    current_period = settlements[0] if settlements else None
    # Prefer the latest period chronologically for the "current 학기" label.
    if settlements:
        current_period = max(
            settlements,
            key=lambda s: (s.academic_year, s.semester),
        )

    recent = settlements[:recent_limit]
    rollups = await _rollups_for_settlements(
        session, settlements=recent, orgs_by_id={org.id: org}
    )
    treasurer_work = [
        TreasurerWorkCard(
            settlement=r.settlement,
            evidence_count=r.evidence_count,
            total_expense=r.total_evidence_amount,
            progress_percent=r.progress_percent,
            last_activity_at=_settlement_last_activity(r.settlement),
        )
        for r in rollups
    ]

    auditor_activity = await _auditor_activity_cards(
        session,
        org=org,
        memberships=memberships,
        users_by_id=users_by_id,
        settlements=settlements,
    )

    return PresidentSummary(
        organization=org,
        president_name=president_name,
        current_period=current_period,
        team_member_count=len(member_user_ids),
        submitted_settlement_count=submitted_count,
        audit_completed_count=audit_completed_count,
        review_pending_count=review_pending_count,
        treasurer_work=treasurer_work,
        auditor_activity=auditor_activity,
        members=members,
    )


async def _auditor_activity_cards(
    session: AsyncSession,
    *,
    org: Organization,
    memberships: Sequence[OrganizationMembership],
    users_by_id: dict[uuid.UUID, User],
    settlements: Sequence[Settlement],
) -> list[AuditorActivityCard]:
    auditor_memberships = [
        m for m in memberships if m.role == RoleType.AUDITOR
    ]
    if not auditor_memberships:
        return []

    settlement_by_id = {s.id: s for s in settlements}
    membership_user = {m.id: m.user_id for m in auditor_memberships}

    # Audit comments authored by this org's auditors are the only per-auditor
    # signal. Map: auditor user_id -> {settlement_id -> latest comment time}.
    reviewed: dict[uuid.UUID, dict[uuid.UUID, datetime]] = {
        m.user_id: {} for m in auditor_memberships
    }
    if settlement_by_id:
        comment_rows = await session.execute(
            select(
                AuditComment.author_membership_id,
                AuditComment.settlement_id,
                AuditComment.created_at,
            )
            .where(
                AuditComment.settlement_id.in_(list(settlement_by_id.keys())),
                AuditComment.author_membership_id.in_(
                    [m.id for m in auditor_memberships]
                ),
            )
        )
        for membership_id, settlement_id, created_at in comment_rows.all():
            user_id = membership_user.get(membership_id)
            if user_id is None:
                continue
            bucket = reviewed[user_id]
            existing = bucket.get(settlement_id)
            if existing is None or (created_at and created_at > existing):
                bucket[settlement_id] = created_at

    cards: list[AuditorActivityCard] = []
    # Dedupe auditors by user (a user could hold the role once per org anyway).
    seen_users: set[uuid.UUID] = set()
    for m in auditor_memberships:
        if m.user_id in seen_users:
            continue
        seen_users.add(m.user_id)
        user = users_by_id.get(m.user_id)
        if user is None:
            continue

        reviewed_settlements = reviewed.get(m.user_id, {})
        completed = 0
        pending = 0
        review_days: list[float] = []
        last_activity: datetime | None = None
        for sid, commented_at in reviewed_settlements.items():
            settlement = settlement_by_id.get(sid)
            if settlement is None:
                continue
            if commented_at is not None and (
                last_activity is None or commented_at > last_activity
            ):
                last_activity = commented_at
            if settlement.status in _AUDIT_COMPLETED_STATUSES:
                completed += 1
                if (
                    settlement.submitted_at is not None
                    and settlement.audited_at is not None
                ):
                    delta = settlement.audited_at - settlement.submitted_at
                    review_days.append(round(delta.total_seconds() / 86400, 1))
            elif settlement.status in _REVIEW_PENDING_STATUSES:
                pending += 1

        avg_review_days = (
            round(sum(review_days) / len(review_days), 1) if review_days else None
        )
        if last_activity is None:
            last_activity = m.created_at

        cards.append(
            AuditorActivityCard(
                user=user,
                assigned_count=completed + pending,
                completed_count=completed,
                pending_count=pending,
                avg_review_days=avg_review_days,
                last_activity_at=last_activity,
            )
        )
    return cards
