"""Settlement artifact endpoints (spec §2 Step 5).

Routes:
  POST /settlements/{id}/artifacts:generate    — produce Excel + PDF pair
  GET  /settlements/{id}/artifacts             — list artifacts
  GET  /artifacts/{id}/download                — download a file

Generation runs synchronously inside the request. Student-org workloads
finish in seconds; if a future evidence batch grows huge, swap this for a
background task without touching the API surface (the row-level QUEUED →
PROCESSING → COMPLETED transitions are already in place).
"""

from __future__ import annotations

import asyncio
import mimetypes
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user, require_membership_for_org
from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Settlement
from union_ledger.models.enums import RoleType
from union_ledger.schemas.artifact import (
    ArtifactGenerationResponse,
    SettlementArtifactResponse,
)
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.services.artifact import (
    ArtifactFileMissing,
    ArtifactNotFound,
    ArtifactNotReady,
    generate_settlement_artifacts,
    get_artifact_or_raise,
    list_settlement_artifacts,
    resolve_download,
)
from union_ledger.services.settlement import (
    SettlementNotFound,
    get_settlement_or_raise,
)

router = APIRouter(tags=["artifacts"])
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
    "/settlements/{settlement_id}/artifacts:generate",
    response_model=ArtifactGenerationResponse,
    summary="결산안 Excel + 증빙 PDF 생성 (Treasurer/Admin)",
)
async def generate_artifacts(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> ArtifactGenerationResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )

    # Run the IO-heavy work off the event loop. The orchestrator itself does
    # async DB work, so we can't push the whole call into a thread; we do
    # let the heavy file IO happen inline because openpyxl/PIL/pypdf are
    # already CPU/IO-bound utilities. asyncio.to_thread on the inner
    # rendering helpers would be a future optimization.
    outcome = await generate_settlement_artifacts(
        session, settings=get_settings(), settlement=settlement
    )
    return ArtifactGenerationResponse(
        excel=SettlementArtifactResponse.model_validate(outcome.excel),
        pdf=SettlementArtifactResponse.model_validate(outcome.pdf),
    )


@router.get(
    "/settlements/{settlement_id}/artifacts",
    response_model=list[SettlementArtifactResponse],
    summary="결산안 산출물 목록 (멤버 전용)",
)
async def list_artifacts(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[SettlementArtifactResponse]:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    rows = await list_settlement_artifacts(session, settlement_id=settlement.id)
    return [SettlementArtifactResponse.model_validate(r) for r in rows]


@router.get(
    "/artifacts/{artifact_id}/download",
    summary="산출물 다운로드 (멤버 전용)",
    response_class=FileResponse,
)
async def download_artifact(
    artifact_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> FileResponse:
    try:
        artifact = await get_artifact_or_raise(session, artifact_id)
    except ArtifactNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    settlement = await _load_settlement_or_404(session, artifact.settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )

    try:
        path = await asyncio.to_thread(resolve_download, artifact)
    except ArtifactNotReady as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ArtifactFileMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(exc),
        ) from exc

    media_type, _ = mimetypes.guess_type(path.name)
    if media_type is None:
        media_type = "application/octet-stream"
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=f"settlement-{settlement.id}-{artifact.artifact_type.value}{path.suffix}",
    )
