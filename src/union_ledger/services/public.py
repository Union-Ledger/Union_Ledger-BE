"""Public Viewer services (spec §4).

A "public" settlement is one where `status == APPROVED` AND
`published_at IS NOT NULL`. The two are not redundant: an admin must call
the explicit `POST /settlements/{id}/publish` after approval to opt-in to
the public view, even though the lifecycle uses APPROVED for both
"audit-passed but not yet released" and "released".

Visibility scoping (spec §1: "소속 단과대 결산안만 열람"):
  We filter the public listing by college_name matching ANY of the caller's
  membership colleges (signup-org college + any invited orgs). This is a
  permissive read of the spec — every user has a signup-org so non-empty
  scoping is guaranteed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    Evidence,
    Organization,
    OrganizationMembership,
    Settlement,
    SettlementArtifact,
    User,
    evidence_signed_amount,
)
from union_ledger.models.enums import ArtifactStatus, SettlementStatus


class PublicError(Exception):
    """Base — maps to HTTP 4xx."""


class SettlementNotPublic(PublicError):
    """Tried to access a settlement that isn't APPROVED+published."""


class EvidenceNotPublic(PublicError):
    """The evidence's settlement isn't public."""


class ArtifactNotPublic(PublicError):
    pass


@dataclass(slots=True)
class PublicSummary:
    settlement: Settlement
    organization: Organization
    total_amount: Decimal
    item_count: int


@dataclass(slots=True)
class PublicDetail:
    settlement: Settlement
    organization: Organization
    total_amount: Decimal
    item_count: int
    artifacts: Sequence[SettlementArtifact]


def is_settlement_public(settlement: Settlement) -> bool:
    return (
        settlement.status == SettlementStatus.APPROVED
        and settlement.published_at is not None
    )


async def user_visible_colleges(
    session: AsyncSession, *, user_id: uuid.UUID
) -> set[str]:
    """Every college name the user may browse published settlements for."""
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
    # Plain students hold no org membership; fall back to the college on their
    # account so they can still browse their college's published settlements.
    user = await session.get(User, user_id)
    if user is not None and user.college_name:
        colleges.add(user.college_name)
    return colleges


async def _settlement_aggregates(
    session: AsyncSession, *, settlement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, int]]:
    """Per-settlement (sum(abs(amount)), evidence_count) at publish snapshot."""
    if not settlement_ids:
        return {}
    rows = await session.execute(
        select(
            Evidence.settlement_id,
            func.coalesce(func.sum(evidence_signed_amount()), 0),
            func.count(Evidence.id),
        )
        .join(Settlement, Settlement.id == Evidence.settlement_id)
        .where(
            Evidence.settlement_id.in_(settlement_ids),
            Settlement.published_at.is_not(None),
            Evidence.created_at <= Settlement.published_at,
        )
        .group_by(Evidence.settlement_id)
    )
    return {
        row[0]: (Decimal(row[1]), int(row[2])) for row in rows.all()
    }


# --- Listing -------------------------------------------------------------


async def list_public_settlements(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[PublicSummary]:
    colleges = await user_visible_colleges(session, user_id=user_id)
    if not colleges:
        return []

    settlements_result = await session.scalars(
        select(Settlement)
        .join(Organization, Organization.id == Settlement.organization_id)
        .where(
            Settlement.status == SettlementStatus.APPROVED,
            Settlement.published_at.is_not(None),
            Organization.college_name.in_(colleges),
        )
        .order_by(Settlement.published_at.desc())
    )
    settlements = list(settlements_result.all())
    if not settlements:
        return []

    org_ids = {s.organization_id for s in settlements}
    orgs_result = await session.scalars(
        select(Organization).where(Organization.id.in_(org_ids))
    )
    org_by_id = {org.id: org for org in orgs_result.all()}

    aggregates = await _settlement_aggregates(
        session, settlement_ids=[s.id for s in settlements]
    )

    return [
        PublicSummary(
            settlement=s,
            organization=org_by_id[s.organization_id],
            total_amount=aggregates.get(s.id, (Decimal(0), 0))[0],
            item_count=aggregates.get(s.id, (Decimal(0), 0))[1],
        )
        for s in settlements
    ]


# --- Detail / items ------------------------------------------------------


async def get_public_settlement_detail(
    session: AsyncSession,
    *,
    settlement: Settlement,
    user_id: uuid.UUID,
) -> PublicDetail:
    """Caller has already loaded the settlement; we enforce public + scope."""
    if not is_settlement_public(settlement):
        raise SettlementNotPublic("아직 공개되지 않은 결산안입니다.")

    org = await session.get(Organization, settlement.organization_id)
    if org is None:
        raise SettlementNotPublic("조직 정보를 찾을 수 없습니다.")

    colleges = await user_visible_colleges(session, user_id=user_id)
    if org.college_name not in colleges:
        raise SettlementNotPublic("열람 권한이 없는 결산안입니다.")

    aggregates = await _settlement_aggregates(
        session, settlement_ids=[settlement.id]
    )
    total, count = aggregates.get(settlement.id, (Decimal(0), 0))

    artifacts = (
        await session.scalars(
            select(SettlementArtifact)
            .where(
                SettlementArtifact.settlement_id == settlement.id,
                SettlementArtifact.status == ArtifactStatus.COMPLETED,
            )
            .order_by(SettlementArtifact.created_at.desc())
        )
    ).all()

    return PublicDetail(
        settlement=settlement,
        organization=org,
        total_amount=total,
        item_count=count,
        artifacts=list(artifacts),
    )


async def list_public_items(
    session: AsyncSession,
    *,
    settlement: Settlement,
    user_id: uuid.UUID,
) -> Sequence[Evidence]:
    if not is_settlement_public(settlement):
        raise SettlementNotPublic("아직 공개되지 않은 결산안입니다.")
    org = await session.get(Organization, settlement.organization_id)
    colleges = await user_visible_colleges(session, user_id=user_id)
    if org is None or org.college_name not in colleges:
        raise SettlementNotPublic("열람 권한이 없는 결산안입니다.")

    rows = await session.scalars(
        select(Evidence)
        .where(
            Evidence.settlement_id == settlement.id,
            Evidence.created_at <= settlement.published_at,
        )
        .order_by(
            Evidence.evidence_date.asc().nullslast(),
            Evidence.created_at.asc(),
        )
    )
    return list(rows.all())


# --- Single evidence -----------------------------------------------------


async def get_public_evidence(
    session: AsyncSession,
    *,
    evidence: Evidence,
    user_id: uuid.UUID,
) -> Evidence:
    settlement = await session.get(Settlement, evidence.settlement_id)
    if settlement is None or not is_settlement_public(settlement):
        raise EvidenceNotPublic("이 증빙은 공개된 결산안에 속하지 않습니다.")
    org = await session.get(Organization, settlement.organization_id)
    colleges = await user_visible_colleges(session, user_id=user_id)
    if org is None or org.college_name not in colleges:
        raise EvidenceNotPublic("열람 권한이 없는 증빙입니다.")
    if (
        settlement.published_at is not None
        and evidence.created_at > settlement.published_at
    ):
        raise EvidenceNotPublic("이 증빙은 공개된 결산안에 속하지 않습니다.")
    return evidence


# --- Artifact gate -------------------------------------------------------


async def assert_artifact_public(
    session: AsyncSession,
    *,
    artifact: SettlementArtifact,
    user_id: uuid.UUID,
) -> None:
    settlement = await session.get(Settlement, artifact.settlement_id)
    if settlement is None or not is_settlement_public(settlement):
        raise ArtifactNotPublic("공개된 결산안의 산출물이 아닙니다.")
    if artifact.status != ArtifactStatus.COMPLETED:
        raise ArtifactNotPublic("산출물이 아직 준비되지 않았습니다.")
    org = await session.get(Organization, settlement.organization_id)
    colleges = await user_visible_colleges(session, user_id=user_id)
    if org is None or org.college_name not in colleges:
        raise ArtifactNotPublic("열람 권한이 없는 산출물입니다.")
