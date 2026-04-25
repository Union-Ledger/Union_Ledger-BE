"""Audit comment service.

Comments are attached to a settlement and optionally to a specific evidence
row inside that settlement. Authorship is recorded via OrganizationMembership
(not User) so the role at comment-time is preserved even if the user later
changes role.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import AuditComment, Evidence, OrganizationMembership


class CommentError(Exception):
    """Base for comment-layer errors (maps to HTTP 4xx)."""


class EvidenceNotInSettlement(CommentError):
    """Raised when `evidence_id` doesn't belong to the target settlement."""


async def create_comment(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
    author_membership: OrganizationMembership | None,
    comment: str,
    evidence_id: uuid.UUID | None = None,
) -> AuditComment:
    if evidence_id is not None:
        # Cross-settlement linkage is a security/UX bug — verify FK alignment.
        evidence = await session.get(Evidence, evidence_id)
        if evidence is None or evidence.settlement_id != settlement_id:
            raise EvidenceNotInSettlement(
                "지정한 증빙은 이 결산안에 속하지 않습니다."
            )

    audit_comment = AuditComment(
        settlement_id=settlement_id,
        evidence_id=evidence_id,
        author_membership_id=(
            author_membership.id if author_membership is not None else None
        ),
        comment=comment,
    )
    session.add(audit_comment)
    await session.commit()
    await session.refresh(audit_comment)
    return audit_comment


async def list_comments(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
) -> Sequence[AuditComment]:
    stmt = (
        select(AuditComment)
        .where(AuditComment.settlement_id == settlement_id)
        .order_by(AuditComment.created_at.asc())
    )
    result = await session.scalars(stmt)
    return list(result.all())
