"""Settlement endpoints (spec ④).

Routing layout:
  - Org-scoped collection routes (`/organizations/{org_id}/settlements`) use
    `require_roles_in_org` because the org id is in the path.
  - Item routes (`/settlements/{id}`) don't carry the org id, so we resolve
    the settlement first and then run an inline membership check against the
    settlement's `organization_id`. This keeps URLs simple for the FE.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import (
    get_current_user,
    require_membership_for_org,
    require_roles_in_org,
)
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationMembership, Settlement
from union_ledger.models.enums import RoleType, SettlementStatus
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.settlement import (
    CategoryBreakdownItem,
    SettlementCreateRequest,
    SettlementExpenseSummaryResponse,
    SettlementResponse,
    SettlementUpdateRequest,
)
from union_ledger.services.settlement import (
    SettlementNotEditable,
    SettlementNotFound,
    TemplateNotFound,
    create_settlement,
    get_settlement_expense_summary,
    get_settlement_or_raise,
    list_org_settlements,
    update_settlement,
)

router = APIRouter(tags=["settlements"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
TreasurerOrAdmin = Annotated[
    OrganizationMembership,
    Depends(require_roles_in_org(RoleType.TREASURER, RoleType.ADMIN)),
]
AnyOrgMember = Annotated[
    OrganizationMembership,
    Depends(require_roles_in_org()),
]


async def _load_settlement_or_404(
    session: AsyncSession,
    settlement_id: uuid.UUID,
) -> Settlement:
    try:
        return await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/organizations/{organization_id}/settlements",
    response_model=SettlementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="결산안 생성 (Treasurer/Admin)",
)
async def create_org_settlement(
    organization_id: uuid.UUID,
    payload: SettlementCreateRequest,
    _membership: TreasurerOrAdmin,
    session: DbSession,
) -> SettlementResponse:
    try:
        settlement = await create_settlement(
            session,
            organization_id=organization_id,
            title=payload.title,
            academic_year=payload.academic_year,
            semester=payload.semester,
            template_id=payload.template_id,
        )
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return SettlementResponse.model_validate(settlement)


@router.get(
    "/organizations/{organization_id}/settlements",
    response_model=list[SettlementResponse],
    summary="조직 결산안 목록 (멤버 전용)",
)
async def list_org_settlements_endpoint(
    organization_id: uuid.UUID,
    _membership: AnyOrgMember,
    session: DbSession,
    status_filter: Annotated[
        SettlementStatus | None,
        Query(alias="status", description="결산안 상태 필터"),
    ] = None,
) -> list[SettlementResponse]:
    items = await list_org_settlements(
        session,
        organization_id=organization_id,
        status_filter=status_filter,
    )
    return [SettlementResponse.model_validate(item) for item in items]


@router.get(
    "/settlements/{settlement_id}",
    response_model=SettlementResponse,
    summary="결산안 상세 (멤버 전용)",
)
async def get_settlement(
    settlement_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    return SettlementResponse.model_validate(settlement)


@router.get(
    "/settlements/{settlement_id}/expense-summary",
    response_model=SettlementExpenseSummaryResponse,
    summary="결산안 지출 집계 (멤버 전용)",
)
async def get_settlement_expense_summary_endpoint(
    settlement_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementExpenseSummaryResponse:
    """Returns total count/amount and per-`budget_category` breakdown.

    Used by the 결산안 생성 page so the FE can render the summary card and
    category list with a single call instead of fetching every evidence and
    aggregating client-side.
    """
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    summary = await get_settlement_expense_summary(session, settlement=settlement)
    return SettlementExpenseSummaryResponse(
        settlement_id=summary.settlement_id,
        total_count=summary.total_count,
        total_amount=summary.total_amount,
        by_category=[
            CategoryBreakdownItem(
                category=item.category,
                count=item.count,
                amount=item.amount,
            )
            for item in summary.by_category
        ],
    )


@router.patch(
    "/settlements/{settlement_id}",
    response_model=SettlementResponse,
    summary="결산안 수정 (DRAFT only, Treasurer/Admin)",
)
async def patch_settlement(
    settlement_id: uuid.UUID,
    payload: SettlementUpdateRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.ADMIN},
    )

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return SettlementResponse.model_validate(settlement)

    try:
        settlement = await update_settlement(
            session,
            settlement=settlement,
            updates=updates,
        )
    except SettlementNotEditable as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return SettlementResponse.model_validate(settlement)
