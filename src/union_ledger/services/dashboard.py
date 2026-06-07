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
)
from union_ledger.models.enums import MatchStatus, RoleType, SettlementStatus

# Roles that get the treasurer dashboard. Admins manage the org and need
# the same visibility, so we lump them in.
_TREASURER_DASHBOARD_ROLES = {RoleType.TREASURER, RoleType.ADMIN}

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
