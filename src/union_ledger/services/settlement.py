"""Settlement (정산안) services.

Spec ④. A Settlement is the top-level container for a single closing period
(e.g., "2026 1학기"). Treasurers create them in DRAFT status, attach evidence
and bank statements, then submit for audit. This module covers the basic
lifecycle CRUD; status transitions (submit, approve, reject, publish) live
with the audit-flow services in a later slice.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import Settlement, SettlementTemplate
from union_ledger.models.enums import SettlementStatus


class SettlementError(Exception):
    """Base for settlement-layer errors (maps to HTTP 4xx)."""


class SettlementNotFound(SettlementError):
    pass


class SettlementNotEditable(SettlementError):
    """Raised when attempting to mutate a non-DRAFT settlement."""


class TemplateNotFound(SettlementError):
    pass


async def create_settlement(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    title: str,
    academic_year: int,
    semester: str,
    template_id: uuid.UUID | None = None,
) -> Settlement:
    if template_id is not None:
        # Verify the template belongs to the same org — prevents cross-org
        # template attachment via FK injection from the request body.
        template = await session.get(SettlementTemplate, template_id)
        if template is None or template.organization_id != organization_id:
            raise TemplateNotFound("템플릿을 찾을 수 없습니다.")

    settlement = Settlement(
        organization_id=organization_id,
        template_id=template_id,
        title=title,
        academic_year=academic_year,
        semester=semester,
        status=SettlementStatus.DRAFT,
    )
    session.add(settlement)
    await session.commit()
    await session.refresh(settlement)
    return settlement


async def list_org_settlements(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    status_filter: SettlementStatus | None = None,
) -> Sequence[Settlement]:
    stmt = (
        select(Settlement)
        .where(Settlement.organization_id == organization_id)
        .order_by(Settlement.academic_year.desc(), Settlement.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Settlement.status == status_filter)
    result = await session.scalars(stmt)
    return list(result.all())


async def get_settlement_or_raise(
    session: AsyncSession,
    settlement_id: uuid.UUID,
) -> Settlement:
    settlement = await session.get(Settlement, settlement_id)
    if settlement is None:
        raise SettlementNotFound("결산안을 찾을 수 없습니다.")
    return settlement


async def update_settlement(
    session: AsyncSession,
    *,
    settlement: Settlement,
    updates: dict[str, Any],
) -> Settlement:
    """Apply an update dict to a settlement.

    Caller (the endpoint) is responsible for filtering `updates` to only the
    fields the schema allows. This service enforces the DRAFT-only rule and
    the cross-org template guard.
    """
    if settlement.status != SettlementStatus.DRAFT:
        raise SettlementNotEditable(
            "DRAFT 상태의 결산안만 수정할 수 있습니다."
        )

    if "template_id" in updates and updates["template_id"] is not None:
        template = await session.get(SettlementTemplate, updates["template_id"])
        if template is None or template.organization_id != settlement.organization_id:
            raise TemplateNotFound("템플릿을 찾을 수 없습니다.")

    for field, value in updates.items():
        setattr(settlement, field, value)

    await session.commit()
    await session.refresh(settlement)
    return settlement
