"""Reconciliation endpoints (spec §2 Step 3–4).

Routes:
  POST   /settlements/{id}/reconciliation:run   — auto-match
  GET    /settlements/{id}/reconciliation       — list results (color codes)
  PATCH  /reconciliation/{matchId}              — manual adjustment

The PATCH endpoint receives the {matchId} alone and resolves the org via
the loaded result's settlement → settlement.organization_id.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user, require_membership_for_org
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Settlement
from union_ledger.models.enums import MatchStatus, RoleType
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.reconciliation import (
    ReconciliationResultResponse,
    ReconciliationRunResponse,
    ReconciliationUpdateRequest,
)
from union_ledger.services.reconciliation import (
    ReconciliationResultMismatch,
    ReconciliationResultNotFound,
    get_result_or_raise,
    list_results,
    run_reconciliation,
    update_result,
)
from union_ledger.services.settlement import (
    SettlementNotFound,
    get_settlement_or_raise,
)

router = APIRouter(tags=["reconciliation"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def _load_settlement_or_404(
    session: AsyncSession, settlement_id: uuid.UUID
) -> Settlement:
    try:
        return await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/settlements/{settlement_id}/reconciliation:run",
    response_model=ReconciliationRunResponse,
    summary="증빙↔거래내역 자동 대조 실행 (Treasurer/Admin)",
)
async def run_match(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> ReconciliationRunResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.ADMIN},
    )
    summary = await run_reconciliation(session, settlement_id=settlement.id)
    return ReconciliationRunResponse(
        settlement_id=summary.settlement_id,
        total=summary.total,
        matched=summary.matched,
        amount_mismatch=summary.amount_mismatch,
        date_mismatch=summary.date_mismatch,
        missing_bank_transaction=summary.missing_bank_transaction,
        missing_evidence=summary.missing_evidence,
        results=[
            ReconciliationResultResponse.model_validate(row) for row in summary.results
        ],
    )


@router.get(
    "/settlements/{settlement_id}/reconciliation",
    response_model=list[ReconciliationResultResponse],
    summary="대조 결과 목록 (멤버 전용)",
)
async def list_settlement_results(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    status_filter: Annotated[
        MatchStatus | None,
        Query(alias="status", description="매칭 상태 필터"),
    ] = None,
) -> list[ReconciliationResultResponse]:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    rows = await list_results(
        session,
        settlement_id=settlement.id,
        status_filter=status_filter,
    )
    return [ReconciliationResultResponse.model_validate(r) for r in rows]


@router.patch(
    "/reconciliation/{match_id}",
    response_model=ReconciliationResultResponse,
    summary="대조 결과 수동 조정 (Treasurer/Admin)",
)
async def patch_match(
    match_id: uuid.UUID,
    payload: ReconciliationUpdateRequest,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> ReconciliationResultResponse:
    try:
        result = await get_result_or_raise(session, match_id)
    except ReconciliationResultNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    settlement = await _load_settlement_or_404(session, result.settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.ADMIN},
    )

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return ReconciliationResultResponse.model_validate(result)

    try:
        result = await update_result(session, result=result, updates=updates)
    except ReconciliationResultMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ReconciliationResultResponse.model_validate(result)
