"""회장(admin) application endpoints.

Flow:
  POST   /admin-applications                     — submit application + proof docs (any user)
  GET    /admin-applications/me                   — my applications
  GET    /admin-applications                      — list (operator), optional ?status=
  GET    /admin-applications/{id}                 — detail (operator or owner)
  GET    /admin-applications/{id}/documents/{i}   — download a proof doc (operator or owner)
  POST   /admin-applications/{id}/approve         — approve → create org + ADMIN (operator)
  POST   /admin-applications/{id}/reject          — reject with a note (operator)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user, require_operator
from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationAdminApplication, User
from union_ledger.models.enums import AdminApplicationStatus
from union_ledger.schemas.admin_application import (
    AdminApplicationApproveResponse,
    AdminApplicationDocumentInfo,
    AdminApplicationResponse,
    AdminApplicationReviewRequest,
)
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.services.admin_application import (
    AdminApplicationNotFound,
    AdminApplicationNotPending,
    approve_application,
    create_application,
    get_application_or_raise,
    list_applications,
    list_applications_for_user,
    reject_application,
)
from union_ledger.services.file_storage import LocalFileStorage

router = APIRouter(tags=["admin-applications"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _is_operator(user: AuthUser) -> bool:
    operator_emails = {email.lower() for email in get_settings().operator_emails}
    return bool(operator_emails) and user.email.lower() in operator_emails


def _to_response(
    application: OrganizationAdminApplication,
    applicant: User | AuthUser | None = None,
) -> AdminApplicationResponse:
    return AdminApplicationResponse(
        id=application.id,
        applicant_user_id=application.applicant_user_id,
        applicant_name=getattr(applicant, "name", None),
        applicant_email=getattr(applicant, "email", None),
        organization_name=application.organization_name,
        college_name=application.college_name,
        department_name=application.department_name,
        documents=[
            AdminApplicationDocumentInfo(
                file_name=str(doc.get("file_name", "")),
                content_type=doc.get("content_type"),
                size=int(doc.get("size", 0) or 0),
            )
            for doc in (application.documents or [])
        ],
        status=application.status,
        review_note=application.review_note,
        reviewed_at=application.reviewed_at,
        created_organization_id=application.created_organization_id,
        created_at=application.created_at,
    )


async def _applicants_by_id(
    session: AsyncSession,
    applications: list[OrganizationAdminApplication],
) -> dict[uuid.UUID, User]:
    ids = {application.applicant_user_id for application in applications}
    if not ids:
        return {}
    result = await session.scalars(select(User).where(User.id.in_(ids)))
    return {user.id: user for user in result.all()}


async def _load_or_404(
    session: AsyncSession, application_id: uuid.UUID
) -> OrganizationAdminApplication:
    try:
        return await get_application_or_raise(session, application_id)
    except AdminApplicationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


def _require_operator_or_owner(
    current_user: AuthUser, application: OrganizationAdminApplication
) -> None:
    if _is_operator(current_user) or application.applicant_user_id == current_user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="이 신청을 조회할 권한이 없습니다.",
    )


@router.post(
    "/admin-applications",
    response_model=AdminApplicationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="회장 신청 + 증빙 서류 제출",
)
async def submit_application(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    organization_name: Annotated[str, Form()],
    college_name: Annotated[str, Form()],
    department_name: Annotated[str, Form()],
    documents: Annotated[list[UploadFile], File()],
) -> AdminApplicationResponse:
    real_files = [f for f in documents if f.filename]
    if not real_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="증빙 서류를 최소 한 개 이상 첨부해 주세요.",
        )

    # Validate every filename up front so we don't persist a partial set.
    for upload in real_files:
        try:
            LocalFileStorage.validate_filename(upload.filename or "")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    storage = LocalFileStorage(get_settings())
    stored_files = [
        await storage.save_admin_application_file(current_user.id, upload)
        for upload in real_files
    ]

    application = await create_application(
        session,
        applicant_id=current_user.id,
        organization_name=organization_name,
        college_name=college_name,
        department_name=department_name,
        stored_files=stored_files,
    )
    return _to_response(application, current_user)


@router.get(
    "/admin-applications/me",
    response_model=list[AdminApplicationResponse],
    summary="내 회장 신청 목록",
)
async def list_my_applications(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> list[AdminApplicationResponse]:
    applications = await list_applications_for_user(
        session, applicant_id=current_user.id
    )
    return [_to_response(application, current_user) for application in applications]


@router.get(
    "/admin-applications",
    response_model=list[AdminApplicationResponse],
    summary="회장 신청 목록 (운영자)",
)
async def list_all_applications(
    session: DbSession,
    _operator: Annotated[AuthUser, Depends(require_operator)],
    application_status: Annotated[AdminApplicationStatus | None, Query(alias="status")] = None,
) -> list[AdminApplicationResponse]:
    applications = await list_applications(session, status=application_status)
    applicants = await _applicants_by_id(session, applications)
    return [
        _to_response(application, applicants.get(application.applicant_user_id))
        for application in applications
    ]


@router.get(
    "/admin-applications/{application_id}",
    response_model=AdminApplicationResponse,
    summary="회장 신청 상세 (운영자 또는 신청자 본인)",
)
async def get_application(
    application_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> AdminApplicationResponse:
    application = await _load_or_404(session, application_id)
    _require_operator_or_owner(current_user, application)
    applicant = await session.get(User, application.applicant_user_id)
    return _to_response(application, applicant)


@router.get(
    "/admin-applications/{application_id}/documents/{index}",
    summary="증빙 서류 다운로드 (운영자 또는 신청자 본인)",
)
async def download_document(
    application_id: uuid.UUID,
    index: int,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> FileResponse:
    application = await _load_or_404(session, application_id)
    _require_operator_or_owner(current_user, application)

    documents = application.documents or []
    if index < 0 or index >= len(documents):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 서류를 찾을 수 없습니다.",
        )
    document = documents[index]
    file_path = Path(str(document.get("file_path", "")))
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="서류 파일을 찾을 수 없습니다.",
        )
    return FileResponse(
        file_path,
        filename=document.get("file_name"),
        media_type=document.get("content_type") or "application/octet-stream",
    )


@router.post(
    "/admin-applications/{application_id}/approve",
    response_model=AdminApplicationApproveResponse,
    summary="회장 신청 승인 → 조직 생성 + ADMIN 부여 (운영자)",
)
async def approve(
    application_id: uuid.UUID,
    session: DbSession,
    operator: Annotated[AuthUser, Depends(require_operator)],
    payload: AdminApplicationReviewRequest | None = None,
) -> AdminApplicationApproveResponse:
    application = await _load_or_404(session, application_id)
    note = payload.note if payload else None
    try:
        application, organization = await approve_application(
            session,
            application=application,
            reviewer_id=operator.id,
            note=note,
        )
    except AdminApplicationNotPending as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    applicant = await session.get(User, application.applicant_user_id)
    return AdminApplicationApproveResponse(
        application=_to_response(application, applicant),
        organization_id=organization.id,
    )


@router.post(
    "/admin-applications/{application_id}/reject",
    response_model=AdminApplicationResponse,
    summary="회장 신청 반려 (운영자)",
)
async def reject(
    application_id: uuid.UUID,
    session: DbSession,
    operator: Annotated[AuthUser, Depends(require_operator)],
    payload: AdminApplicationReviewRequest | None = None,
) -> AdminApplicationResponse:
    application = await _load_or_404(session, application_id)
    note = payload.note if payload else None
    try:
        application = await reject_application(
            session,
            application=application,
            reviewer_id=operator.id,
            note=note,
        )
    except AdminApplicationNotPending as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    applicant = await session.get(User, application.applicant_user_id)
    return _to_response(application, applicant)
