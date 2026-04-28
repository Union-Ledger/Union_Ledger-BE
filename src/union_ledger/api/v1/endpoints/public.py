"""Public Viewer endpoints (spec §4 / API spec /public/*).

Routes:
  GET /public/settlements                              — published list, college-scoped
  GET /public/settlements/{id}                         — detail (with public artifacts)
  GET /public/settlements/{id}/items                   — itemized expenses
  GET /public/evidences/{id}                           — evidence metadata
  GET /public/evidences/{id}/file                      — evidence file binary
  GET /public/settlements/{id}/downloads/{artifactId}  — Excel/PDF download

All endpoints require auth — there's no anonymous public access (every user
has a Konkuk account). Visibility is scoped by the caller's college (via
their memberships).
"""

from __future__ import annotations

import asyncio
import mimetypes
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Evidence
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.public import (
    PublicArtifactSummary,
    PublicEvidenceDetail,
    PublicSettlementDetail,
    PublicSettlementItem,
    PublicSettlementSummary,
)
from union_ledger.services.artifact import (
    ArtifactFileMissing,
    ArtifactNotFound,
    ArtifactNotReady,
    get_artifact_or_raise,
    resolve_download,
)
from union_ledger.services.public import (
    ArtifactNotPublic,
    EvidenceNotPublic,
    SettlementNotPublic,
    assert_artifact_public,
    get_public_evidence,
    get_public_settlement_detail,
    list_public_items,
    list_public_settlements,
)
from union_ledger.services.settlement import (
    SettlementNotFound,
    get_settlement_or_raise,
)

router = APIRouter(prefix="/public", tags=["public"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _not_public_error(exc: SettlementNotPublic | EvidenceNotPublic) -> HTTPException:
    # 404 (not 403) — we don't disclose existence of unpublished settlements
    # or evidences outside the caller's college.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/settlements",
    response_model=list[PublicSettlementSummary],
    summary="공개된 결산안 목록 (소속 단과대 한정)",
)
async def list_published(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[PublicSettlementSummary]:
    rows = await list_public_settlements(session, user_id=current_user.id)
    return [
        PublicSettlementSummary(
            id=r.settlement.id,
            organization_id=r.organization.id,
            organization_name=r.organization.name,
            college_name=r.organization.college_name,
            department_name=r.organization.department_name,
            title=r.settlement.title,
            academic_year=r.settlement.academic_year,
            semester=r.settlement.semester,
            published_at=r.settlement.published_at,  # type: ignore[arg-type]
            total_amount=r.total_amount,
            item_count=r.item_count,
        )
        for r in rows
    ]


@router.get(
    "/settlements/{settlement_id}",
    response_model=PublicSettlementDetail,
    summary="공개 결산안 상세",
)
async def get_published_detail(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> PublicSettlementDetail:
    try:
        settlement = await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        detail = await get_public_settlement_detail(
            session, settlement=settlement, user_id=current_user.id
        )
    except SettlementNotPublic as exc:
        raise _not_public_error(exc) from exc

    return PublicSettlementDetail(
        id=detail.settlement.id,
        organization_id=detail.organization.id,
        organization_name=detail.organization.name,
        college_name=detail.organization.college_name,
        department_name=detail.organization.department_name,
        title=detail.settlement.title,
        academic_year=detail.settlement.academic_year,
        semester=detail.settlement.semester,
        submitted_at=detail.settlement.submitted_at,
        audited_at=detail.settlement.audited_at,
        published_at=detail.settlement.published_at,  # type: ignore[arg-type]
        total_amount=detail.total_amount,
        item_count=detail.item_count,
        artifacts=[
            PublicArtifactSummary.model_validate(a) for a in detail.artifacts
        ],
    )


@router.get(
    "/settlements/{settlement_id}/items",
    response_model=list[PublicSettlementItem],
    summary="공개 결산안 항목별 (= 증빙 목록)",
)
async def get_published_items(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[PublicSettlementItem]:
    try:
        settlement = await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        evidences = await list_public_items(
            session, settlement=settlement, user_id=current_user.id
        )
    except SettlementNotPublic as exc:
        raise _not_public_error(exc) from exc

    return [
        PublicSettlementItem(
            evidence_id=ev.id,
            evidence_type=ev.evidence_type,
            evidence_date=ev.evidence_date,
            merchant_name=ev.merchant_name,
            amount=ev.amount,
            payment_method=ev.payment_method,
            budget_category=ev.budget_category,
            has_evidence_file=_has_file(ev.source_file_path),
        )
        for ev in evidences
    ]


@router.get(
    "/evidences/{evidence_id}",
    response_model=PublicEvidenceDetail,
    summary="공개 증빙 메타데이터",
)
async def get_published_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> PublicEvidenceDetail:
    evidence = await session.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="증빙을 찾을 수 없습니다.",
        )
    try:
        await get_public_evidence(
            session, evidence=evidence, user_id=current_user.id
        )
    except EvidenceNotPublic as exc:
        raise _not_public_error(exc) from exc

    return PublicEvidenceDetail(
        id=evidence.id,
        settlement_id=evidence.settlement_id,
        evidence_type=evidence.evidence_type,
        evidence_date=evidence.evidence_date,
        merchant_name=evidence.merchant_name,
        amount=evidence.amount,
        payment_method=evidence.payment_method,
        budget_category=evidence.budget_category,
        source_file_name=evidence.source_file_name,
        has_evidence_file=_has_file(evidence.source_file_path),
    )


@router.get(
    "/evidences/{evidence_id}/file",
    summary="공개 증빙 원본 파일 다운로드/뷰",
    response_class=FileResponse,
)
async def get_published_evidence_file(
    evidence_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> FileResponse:
    evidence = await session.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="증빙을 찾을 수 없습니다.",
        )
    try:
        await get_public_evidence(
            session, evidence=evidence, user_id=current_user.id
        )
    except EvidenceNotPublic as exc:
        raise _not_public_error(exc) from exc

    path = Path(evidence.source_file_path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="증빙 파일이 더 이상 존재하지 않습니다.",
        )
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type is None:
        media_type = "application/octet-stream"
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=evidence.source_file_name,
    )


@router.get(
    "/settlements/{settlement_id}/downloads/{artifact_id}",
    summary="공개 결산안 산출물(Excel/PDF) 다운로드",
    response_class=FileResponse,
)
async def download_public_artifact(
    settlement_id: uuid.UUID,
    artifact_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> FileResponse:
    try:
        artifact = await get_artifact_or_raise(session, artifact_id)
    except ArtifactNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    if artifact.settlement_id != settlement_id:
        # The URL pairs settlement_id + artifact_id; if they don't match,
        # return 404 (not 400) — treat as nonexistent.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="산출물을 찾을 수 없습니다.",
        )

    try:
        await assert_artifact_public(
            session, artifact=artifact, user_id=current_user.id
        )
    except ArtifactNotPublic as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        path = await asyncio.to_thread(resolve_download, artifact)
    except ArtifactNotReady as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except ArtifactFileMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=str(exc)
        ) from exc

    media_type, _ = mimetypes.guess_type(path.name)
    if media_type is None:
        media_type = "application/octet-stream"
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=f"settlement-{settlement_id}-{artifact.artifact_type.value}{path.suffix}",
    )


def _has_file(source_file_path: str | None) -> bool:
    if not source_file_path:
        return False
    try:
        return Path(source_file_path).exists()
    except (OSError, ValueError):
        return False
