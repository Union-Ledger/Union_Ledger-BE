from __future__ import annotations

import asyncio
import mimetypes
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import (
    get_current_user,
    require_membership_for_org,
    require_roles,
)
from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Evidence, Settlement
from union_ledger.models.enums import EvidenceStatus, EvidenceType, RoleType
from union_ledger.schemas.auth import AuthUser
from union_ledger.schemas.evidence import (
    EvidenceResponse,
    EvidenceUpdateRequest,
    OCRPreviewResponse,
)
from union_ledger.services.evidence import (
    EvidenceFileAccessDenied,
    EvidenceNotFound,
    assert_evidence_file_access,
    delete_evidence,
    get_evidence_or_raise,
)
from union_ledger.services.evidence_extraction import (
    RECEIPT_CATEGORIES,
    EvidenceExtractionService,
    ExtractionConfigurationError,
    ExtractionError,
)
from union_ledger.services.file_storage import (
    LocalFileStorage,
    read_upload_within_limit,
)
from union_ledger.services.settlement import (
    SettlementNotEditable,
    assert_settlement_allows_evidence_mutation,
)

router = APIRouter(tags=["evidences", "ocr"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _evidence_mutation_blocked(exc: SettlementNotEditable) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


async def _require_evidence_mutable_settlement(
    session: AsyncSession, settlement_id: uuid.UUID
) -> Settlement:
    settlement = await session.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="결산안을 찾을 수 없습니다.",
        )
    try:
        assert_settlement_allows_evidence_mutation(settlement)
    except SettlementNotEditable as exc:
        raise _evidence_mutation_blocked(exc) from exc
    return settlement


@router.get(
    "/evidences/categories",
    summary="List the fixed expense categories used for receipt auto-classification",
)
async def list_expense_categories(
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> dict[str, list[str]]:
    """Single source of truth for the budget-category list (FE edit dropdown)."""
    del current_user
    return {"categories": list(RECEIPT_CATEGORIES)}


@router.post(
    "/ocr/preview",
    response_model=OCRPreviewResponse,
    summary="Run OCR/PDF text extraction without saving to the database",
)
async def preview_evidence_extraction(
    evidence_type: Annotated[EvidenceType, Form()],
    file: Annotated[UploadFile, File()],
    current_user: Annotated[
        AuthUser,
        Depends(require_roles(RoleType.TREASURER, RoleType.PRESIDENT)),
    ],
) -> OCRPreviewResponse:
    del current_user
    filename = LocalFileStorage.validate_filename(file.filename or "upload.bin")
    content = await read_upload_within_limit(
        file, get_settings().max_upload_size_mb * 1024 * 1024
    )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="비어 있는 파일은 처리할 수 없습니다.",
        )

    extractor = EvidenceExtractionService(get_settings())
    try:
        result = await asyncio.to_thread(extractor.extract_upload, filename, content, evidence_type)
    except ExtractionConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except ExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return OCRPreviewResponse(
        source_file_name=result.source_file_name,
        evidence_type=result.evidence_type,
        status=EvidenceStatus.NEEDS_REVIEW,
        extraction_method=result.method,
        extracted_payload=result.payload,
        evidence_date=result.evidence_date,
        merchant_name=result.merchant_name,
        amount=result.amount,
        payment_method=result.payment_method,
        is_refund=result.is_refund,
    )


@router.post(
    "/settlements/{settlement_id}/evidences",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an evidence file for a settlement",
)
async def upload_evidence(
    settlement_id: uuid.UUID,
    evidence_type: Annotated[EvidenceType, Form()],
    file: Annotated[UploadFile, File()],
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    # 구분 (ledger group, e.g. "중간고사 간식행사") — applied per upload batch.
    group_name: Annotated[str | None, Form()] = None,
    # Category chosen at upload time (empty = AI auto-classify on extract).
    budget_category: Annotated[str | None, Form()] = None,
) -> EvidenceResponse:
    settlement = await session.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="결산안을 찾을 수 없습니다.",
        )
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )
    try:
        assert_settlement_allows_evidence_mutation(settlement)
    except SettlementNotEditable as exc:
        raise _evidence_mutation_blocked(exc) from exc

    storage = LocalFileStorage(get_settings())
    try:
        stored_file = await storage.save_evidence_file(settlement_id, file)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    evidence = Evidence(
        settlement_id=settlement.id,
        organization_id=settlement.organization_id,
        evidence_type=evidence_type,
        source_file_name=stored_file.original_name,
        source_file_path=str(stored_file.absolute_path),
        group_name=(group_name or "").strip() or None,
        budget_category=(budget_category or "").strip() or None,
        extracted_payload={
            "content_type": stored_file.content_type,
            "file_size": stored_file.size,
            "storage_path": stored_file.relative_path.as_posix(),
        },
    )
    session.add(evidence)
    await session.commit()
    await session.refresh(evidence)
    return EvidenceResponse.model_validate(evidence)


@router.get(
    "/settlements/{settlement_id}/evidences",
    response_model=list[EvidenceResponse],
    summary="List uploaded evidences for a settlement",
)
async def list_evidences(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[EvidenceResponse]:
    settlement = await session.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="결산안을 찾을 수 없습니다.",
        )
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    result = await session.scalars(
        select(Evidence)
        .where(Evidence.settlement_id == settlement_id)
        .order_by(Evidence.created_at.desc())
    )
    return [EvidenceResponse.model_validate(evidence) for evidence in result.all()]


@router.get(
    "/evidences/{evidence_id}",
    response_model=EvidenceResponse,
    summary="Get evidence detail",
)
async def get_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> EvidenceResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=evidence.organization_id,
    )
    return EvidenceResponse.model_validate(evidence)


@router.get(
    "/evidences/{evidence_id}/file",
    summary="증빙 원본 파일 (조직 멤버 또는 동일 단과대 감사위원)",
    response_class=FileResponse,
)
async def get_evidence_file(
    evidence_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> FileResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)
    try:
        await assert_evidence_file_access(
            session,
            user_id=current_user.id,
            evidence=evidence,
        )
    except EvidenceFileAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    path = Path(evidence.source_file_path) if evidence.source_file_path else None
    if path is None or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="증빙 원본 파일을 찾을 수 없습니다.",
        )
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path=path,
        media_type=media_type or "application/octet-stream",
        filename=evidence.source_file_name or path.name,
    )


@router.post(
    "/evidences/{evidence_id}/extract",
    response_model=EvidenceResponse,
    summary="Run OCR/PDF extraction for a stored evidence file",
)
async def extract_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> EvidenceResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=evidence.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )
    await _require_evidence_mutable_settlement(session, evidence.settlement_id)
    evidence.status = EvidenceStatus.EXTRACTING
    await session.commit()

    extractor = EvidenceExtractionService(get_settings())
    try:
        result = await asyncio.to_thread(
            extractor.extract_file,
            Path(evidence.source_file_path),
            evidence.evidence_type,
        )
    except ExtractionConfigurationError as exc:
        evidence.status = EvidenceStatus.FAILED
        evidence.extracted_payload = {**(evidence.extracted_payload or {}), "error": str(exc)}
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except ExtractionError as exc:
        evidence.status = EvidenceStatus.FAILED
        evidence.extracted_payload = {**(evidence.extracted_payload or {}), "error": str(exc)}
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    evidence.status = EvidenceStatus.NEEDS_REVIEW
    evidence.extraction_method = result.method
    evidence.extracted_payload = {**(evidence.extracted_payload or {}), **result.payload}
    evidence.evidence_date = result.evidence_date
    evidence.merchant_name = result.merchant_name
    evidence.amount = result.amount
    evidence.payment_method = result.payment_method
    evidence.is_refund = result.is_refund
    # LLM이 판별한 증빙 종류로 갱신(업로드 시점엔 기본값으로 저장됨).
    evidence.evidence_type = result.evidence_type
    # Auto-fill the LLM-inferred category, but never overwrite a category the
    # treasurer already set by hand (the FE may submit one on upload).
    if result.budget_category and not evidence.budget_category:
        evidence.budget_category = result.budget_category
    await session.commit()
    await session.refresh(evidence)
    return EvidenceResponse.model_validate(evidence)


@router.patch(
    "/evidences/{evidence_id}",
    response_model=EvidenceResponse,
    summary="Update extracted evidence fields after manual review",
)
async def update_evidence(
    evidence_id: uuid.UUID,
    payload: EvidenceUpdateRequest,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> EvidenceResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=evidence.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )
    await _require_evidence_mutable_settlement(session, evidence.settlement_id)

    if "evidence_date" in payload.model_fields_set:
        evidence.evidence_date = payload.evidence_date
    if "merchant_name" in payload.model_fields_set:
        evidence.merchant_name = payload.merchant_name
    if "amount" in payload.model_fields_set:
        evidence.amount = payload.amount
    if "payment_method" in payload.model_fields_set:
        evidence.payment_method = payload.payment_method
    if "budget_category" in payload.model_fields_set:
        evidence.budget_category = payload.budget_category
    if "group_name" in payload.model_fields_set:
        evidence.group_name = (payload.group_name or "").strip() or None
    if "is_refund" in payload.model_fields_set and payload.is_refund is not None:
        evidence.is_refund = payload.is_refund
    if "status" in payload.model_fields_set and payload.status is not None:
        evidence.status = payload.status
    if "extracted_payload" in payload.model_fields_set and payload.extracted_payload is not None:
        evidence.extracted_payload = {
            **(evidence.extracted_payload or {}),
            **payload.extracted_payload,
        }

    await session.commit()
    await session.refresh(evidence)
    return EvidenceResponse.model_validate(evidence)


@router.delete(
    "/evidences/{evidence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="증빙 자료 삭제 (Treasurer/Admin)",
)
async def remove_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> None:
    evidence = await _get_evidence_or_404(session, evidence_id)

    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=evidence.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )
    await _require_evidence_mutable_settlement(session, evidence.settlement_id)

    await delete_evidence(session, evidence=evidence)


async def _get_evidence_or_404(session: AsyncSession, evidence_id: uuid.UUID) -> Evidence:
    try:
        return await get_evidence_or_raise(session, evidence_id)
    except EvidenceNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
