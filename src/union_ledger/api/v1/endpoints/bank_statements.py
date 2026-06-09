"""Bank statement endpoints (spec §2 Step 3).

Routes:
  POST   /settlements/{id}/bank-statements         — upload + parse xlsx
  GET    /settlements/{id}/bank-statements         — list uploads
  GET    /settlements/{id}/bank-transactions       — list parsed rows
  DELETE /bank-statements/{upload_id}              — delete one upload + rows

Org auth is resolved off the loaded settlement (item-level routes don't
carry the org id), then checked inline against the caller's memberships.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user, require_membership_for_org
from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Settlement
from union_ledger.models.enums import RoleType
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.bank_statement import (
    BankStatementUploadResponse,
    BankTransactionResponse,
)
from union_ledger.services.bank_statement import (
    BankStatementParseError,
    BankStatementUploadNotFound,
    EmptyBankStatementFile,
    UnsupportedBankStatementFormat,
    create_upload_with_transactions,
    delete_bank_statement_upload,
    get_upload_or_raise,
    list_settlement_transactions,
    list_settlement_uploads,
    parse_bank_statement_bytes,
    save_bank_statement_file,
)
from union_ledger.services.settlement import (
    SettlementNotFound,
    get_settlement_or_raise,
)

router = APIRouter(tags=["bank-statements"])
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
    "/settlements/{settlement_id}/bank-statements",
    response_model=BankStatementUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="거래내역 엑셀 업로드 + 파싱 (Treasurer/Admin)",
)
async def upload_bank_statement(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
) -> BankStatementUploadResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )

    try:
        stored = await save_bank_statement_file(
            get_settings(),
            settlement_id=settlement.id,
            upload_file=file,
        )
    except UnsupportedBankStatementFormat as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except EmptyBankStatementFile as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        transactions = parse_bank_statement_bytes(stored.absolute_path.read_bytes())
    except BankStatementParseError as exc:
        # Persist a FAILED upload record so the FE can surface "parse failed".
        upload = await create_upload_with_transactions(
            session,
            settlement_id=settlement.id,
            stored_file=stored,
            transactions=[],
        )
        # The empty list above auto-marks status as FAILED inside the helper.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    upload = await create_upload_with_transactions(
        session,
        settlement_id=settlement.id,
        stored_file=stored,
        transactions=transactions,
    )
    return BankStatementUploadResponse.model_validate(upload)


@router.get(
    "/settlements/{settlement_id}/bank-statements",
    response_model=list[BankStatementUploadResponse],
    summary="거래내역 업로드 이력 (멤버 전용)",
)
async def list_uploads(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[BankStatementUploadResponse]:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    uploads = await list_settlement_uploads(session, settlement_id=settlement.id)
    return [BankStatementUploadResponse.model_validate(u) for u in uploads]


@router.get(
    "/settlements/{settlement_id}/bank-transactions",
    response_model=list[BankTransactionResponse],
    summary="파싱된 거래내역 목록 (멤버 전용)",
)
async def list_transactions(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[BankTransactionResponse]:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    transactions = await list_settlement_transactions(session, settlement_id=settlement.id)
    return [BankTransactionResponse.model_validate(tx) for tx in transactions]


@router.delete(
    "/bank-statements/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="거래내역 업로드 삭제 (Treasurer/Admin)",
)
async def delete_upload(
    upload_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> None:
    try:
        upload = await get_upload_or_raise(session, upload_id)
    except BankStatementUploadNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    settlement = await _load_settlement_or_404(session, upload.settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )

    await delete_bank_statement_upload(session, upload=upload)
