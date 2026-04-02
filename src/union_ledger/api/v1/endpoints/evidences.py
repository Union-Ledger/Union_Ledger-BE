from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Evidence, Settlement
from union_ledger.models.enums import EvidenceStatus, EvidenceType
from union_ledger.schemas.evidence import (
    EvidenceResponse,
    EvidenceUpdateRequest,
    OCRPreviewResponse,
)
from union_ledger.services.evidence_extraction import (
    EvidenceExtractionService,
    ExtractionConfigurationError,
    ExtractionError,
)
from union_ledger.services.file_storage import LocalFileStorage

router = APIRouter(tags=["evidences", "ocr"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.post(
    "/ocr/preview",
    response_model=OCRPreviewResponse,
    summary="Run OCR/PDF text extraction without saving to the database",
)
async def preview_evidence_extraction(
    evidence_type: Annotated[EvidenceType, Form()],
    file: Annotated[UploadFile, File()],
) -> OCRPreviewResponse:
    filename = LocalFileStorage.validate_filename(file.filename or "upload.bin")
    content = await file.read()
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
) -> EvidenceResponse:
    settlement = await session.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="결산안을 찾을 수 없습니다.",
        )

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
) -> list[EvidenceResponse]:
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
) -> EvidenceResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)
    return EvidenceResponse.model_validate(evidence)


@router.post(
    "/evidences/{evidence_id}/extract",
    response_model=EvidenceResponse,
    summary="Run OCR/PDF extraction for a stored evidence file",
)
async def extract_evidence(
    evidence_id: uuid.UUID,
    session: DbSession,
) -> EvidenceResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)
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
) -> EvidenceResponse:
    evidence = await _get_evidence_or_404(session, evidence_id)

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


async def _get_evidence_or_404(session: AsyncSession, evidence_id: uuid.UUID) -> Evidence:
    evidence = await session.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="증빙 자료를 찾을 수 없습니다.",
        )
    return evidence
