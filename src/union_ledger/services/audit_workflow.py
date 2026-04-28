"""Auditor-side workflow services (spec §3).

Three responsibilities:
  - `list_auditor_worklist`: every settlement in any org where the caller
    holds AUDITOR. We aggregate evidence/transaction/comment counts and a
    reconciliation summary in-process; counts are small enough that
    chasing N+1 is not worth a CTE for the MVP.
  - `get_review_bundle`: one-shot read for the comparison-view screen.
  - `update_comment`: comment edit, gated to the original author.
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

# Settlement statuses an auditor may see in their worklist. DRAFT is
# irrelevant — the treasurer hasn't asked for review yet.
AUDITOR_VISIBLE_STATUSES: frozenset[SettlementStatus] = frozenset(
    {
        SettlementStatus.SUBMITTED,
        SettlementStatus.UNDER_AUDIT,
        SettlementStatus.RESUBMITTED,
        SettlementStatus.APPROVED,
        SettlementStatus.REJECTED,
    }
)


class AuditWorkflowError(Exception):
    """Base error (maps to HTTP 4xx)."""


class CommentNotFound(AuditWorkflowError):
    pass


class CommentNotEditableByUser(AuditWorkflowError):
    """Raised when a non-author tries to edit a comment."""


@dataclass(slots=True)
class WorklistEntry:
    settlement: Settlement
    organization: Organization
    evidence_count: int
    bank_transaction_count: int
    audit_comment_count: int
    total_evidence_amount: Decimal
    reconciliation_counts: dict[MatchStatus, int] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewBundle:
    settlement: Settlement
    evidences: Sequence[Evidence]
    bank_transactions: Sequence[BankTransaction]
    reconciliation_results: Sequence[ReconciliationResult]
    comments: Sequence[AuditComment]


# --- Worklist -------------------------------------------------------------


async def list_auditor_worklist(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    statuses: Sequence[SettlementStatus] | None = None,
) -> list[WorklistEntry]:
    """Settlements visible to a user in any org where they hold AUDITOR.

    `statuses=None` → all auditor-visible statuses (everything except DRAFT).
    Pass `statuses=[SUBMITTED, RESUBMITTED]` for "needs my action".
    """
    target_statuses = (
        set(statuses) if statuses else set(AUDITOR_VISIBLE_STATUSES)
    )
    if not target_statuses:
        return []

    # Orgs where the caller is an auditor.
    auditor_org_ids = await session.scalars(
        select(OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role == RoleType.AUDITOR,
        )
        .distinct()
    )
    org_ids = list(auditor_org_ids.all())
    if not org_ids:
        return []

    settlements_result = await session.scalars(
        select(Settlement)
        .where(
            Settlement.organization_id.in_(org_ids),
            Settlement.status.in_(target_statuses),
        )
        .order_by(
            Settlement.submitted_at.desc().nullslast(),
            Settlement.created_at.desc(),
        )
    )
    settlements = list(settlements_result.all())
    if not settlements:
        return []

    settlement_ids = [s.id for s in settlements]

    # Evidence aggregates: per-settlement count + sum(abs(amount))
    evidence_rows = await session.execute(
        select(
            Evidence.settlement_id,
            func.count(Evidence.id),
            func.coalesce(func.sum(func.abs(Evidence.amount)), 0),
        ).where(Evidence.settlement_id.in_(settlement_ids))
        .group_by(Evidence.settlement_id)
    )
    evidence_summary: dict[uuid.UUID, tuple[int, Decimal]] = {
        row[0]: (int(row[1]), Decimal(row[2])) for row in evidence_rows.all()
    }

    # Bank transaction counts per settlement.
    tx_rows = await session.execute(
        select(BankStatementUpload.settlement_id, func.count(BankTransaction.id))
        .join(BankTransaction, BankTransaction.upload_id == BankStatementUpload.id)
        .where(BankStatementUpload.settlement_id.in_(settlement_ids))
        .group_by(BankStatementUpload.settlement_id)
    )
    tx_count_by_settlement: dict[uuid.UUID, int] = {
        row[0]: int(row[1]) for row in tx_rows.all()
    }

    # Comment counts per settlement.
    comment_rows = await session.execute(
        select(AuditComment.settlement_id, func.count(AuditComment.id))
        .where(AuditComment.settlement_id.in_(settlement_ids))
        .group_by(AuditComment.settlement_id)
    )
    comment_count_by_settlement: dict[uuid.UUID, int] = {
        row[0]: int(row[1]) for row in comment_rows.all()
    }

    # Reconciliation counts grouped by (settlement, match_status).
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

    # Org metadata in one query.
    org_result = await session.scalars(
        select(Organization).where(Organization.id.in_(org_ids))
    )
    org_by_id = {org.id: org for org in org_result.all()}

    entries: list[WorklistEntry] = []
    for settlement in settlements:
        ev_count, ev_total = evidence_summary.get(
            settlement.id, (0, Decimal(0))
        )
        entries.append(
            WorklistEntry(
                settlement=settlement,
                organization=org_by_id[settlement.organization_id],
                evidence_count=ev_count,
                bank_transaction_count=tx_count_by_settlement.get(settlement.id, 0),
                audit_comment_count=comment_count_by_settlement.get(settlement.id, 0),
                total_evidence_amount=ev_total,
                reconciliation_counts=rec_summary.get(settlement.id, {}),
            )
        )
    return entries


# --- Review bundle --------------------------------------------------------


async def get_review_bundle(
    session: AsyncSession, *, settlement: Settlement
) -> ReviewBundle:
    evidences = (
        await session.scalars(
            select(Evidence)
            .where(Evidence.settlement_id == settlement.id)
            .order_by(Evidence.created_at.asc())
        )
    ).all()

    bank_txs = (
        await session.scalars(
            select(BankTransaction)
            .join(
                BankStatementUpload,
                BankTransaction.upload_id == BankStatementUpload.id,
            )
            .where(BankStatementUpload.settlement_id == settlement.id)
            .order_by(BankTransaction.transaction_date.asc())
        )
    ).all()

    rec_results = (
        await session.scalars(
            select(ReconciliationResult)
            .where(ReconciliationResult.settlement_id == settlement.id)
            .order_by(ReconciliationResult.created_at.asc())
        )
    ).all()

    comments = (
        await session.scalars(
            select(AuditComment)
            .where(AuditComment.settlement_id == settlement.id)
            .order_by(AuditComment.created_at.asc())
        )
    ).all()

    return ReviewBundle(
        settlement=settlement,
        evidences=list(evidences),
        bank_transactions=list(bank_txs),
        reconciliation_results=list(rec_results),
        comments=list(comments),
    )


# --- Comment edit ---------------------------------------------------------


async def update_comment(
    session: AsyncSession,
    *,
    comment_id: uuid.UUID,
    user_id: uuid.UUID,
    new_text: str,
) -> AuditComment:
    """Edit a comment. Only the original author may edit.

    Author is identified via the membership.user_id chain — comments record
    `author_membership_id`, so we resolve membership.user_id and compare to
    the caller's user_id.
    """
    comment = await session.get(AuditComment, comment_id)
    if comment is None:
        raise CommentNotFound("코멘트를 찾을 수 없습니다.")

    if comment.author_membership_id is None:
        # Comments without an author (legacy / system) cannot be edited.
        raise CommentNotEditableByUser("이 코멘트는 수정할 수 없습니다.")

    membership = await session.get(
        OrganizationMembership, comment.author_membership_id
    )
    if membership is None or membership.user_id != user_id:
        raise CommentNotEditableByUser("작성자만 코멘트를 수정할 수 있습니다.")

    comment.comment = new_text
    await session.commit()
    await session.refresh(comment)
    return comment
