"""Settlement template endpoints (spec ④ Step 1).

Treasurers/Admins upload an Excel file (xlsx/xls) along with a JSON
`mapping_schema` describing how cells map to spec fields. The mapping schema
can be refined later via PATCH; auto-detection of cell positions is deferred
to a later slice.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import (
    get_current_user,
    require_membership_for_org,
    require_roles_in_org,
)
from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationMembership
from union_ledger.models.enums import RoleType
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.settlement_template import (
    SettlementTemplateResponse,
    SettlementTemplateUpdateRequest,
)
from union_ledger.services.settlement_template import (
    EmptyTemplateFile,
    TemplateNotFound,
    UnsupportedTemplateFormat,
    create_template,
    get_template_or_raise,
    list_org_templates,
    save_template_file,
    update_template,
)

router = APIRouter(tags=["settlement-templates"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
TreasurerOrAdmin = Annotated[
    OrganizationMembership,
    Depends(require_roles_in_org(RoleType.TREASURER, RoleType.ADMIN)),
]
AnyOrgMember = Annotated[
    OrganizationMembership,
    Depends(require_roles_in_org()),
]


def _parse_mapping_schema(raw: str | None) -> dict[str, Any]:
    if raw is None or raw.strip() == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"mapping_schema는 유효한 JSON이어야 합니다: {exc.msg}",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mapping_schema는 JSON object 형태여야 합니다.",
        )
    return parsed


@router.post(
    "/organizations/{organization_id}/templates",
    response_model=SettlementTemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="결산 템플릿 업로드 (Treasurer/Admin)",
)
async def upload_template(
    organization_id: uuid.UUID,
    _membership: TreasurerOrAdmin,
    session: DbSession,
    name: Annotated[str, Form(min_length=1, max_length=255)],
    file: Annotated[UploadFile, File()],
    mapping_schema: Annotated[
        str | None,
        Form(description="JSON object 문자열 (cell → spec field 매핑). 비워두면 빈 dict."),
    ] = None,
) -> SettlementTemplateResponse:
    parsed_mapping = _parse_mapping_schema(mapping_schema)

    try:
        stored = await save_template_file(
            get_settings(),
            organization_id=organization_id,
            upload_file=file,
        )
    except UnsupportedTemplateFormat as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except EmptyTemplateFile as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    template = await create_template(
        session,
        organization_id=organization_id,
        name=name,
        stored_file=stored,
        mapping_schema=parsed_mapping,
    )
    return SettlementTemplateResponse.model_validate(template)


@router.get(
    "/organizations/{organization_id}/templates",
    response_model=list[SettlementTemplateResponse],
    summary="조직 템플릿 목록 (멤버 전용)",
)
async def list_templates(
    organization_id: uuid.UUID,
    _membership: AnyOrgMember,
    session: DbSession,
    include_inactive: bool = False,
) -> list[SettlementTemplateResponse]:
    items = await list_org_templates(
        session,
        organization_id=organization_id,
        include_inactive=include_inactive,
    )
    return [SettlementTemplateResponse.model_validate(item) for item in items]


@router.get(
    "/templates/{template_id}",
    response_model=SettlementTemplateResponse,
    summary="템플릿 상세 (멤버 전용)",
)
async def get_template(
    template_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementTemplateResponse:
    try:
        template = await get_template_or_raise(session, template_id)
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=template.organization_id,
    )
    return SettlementTemplateResponse.model_validate(template)


@router.patch(
    "/templates/{template_id}",
    response_model=SettlementTemplateResponse,
    summary="템플릿 수정 (Treasurer/Admin)",
)
async def patch_template(
    template_id: uuid.UUID,
    payload: SettlementTemplateUpdateRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementTemplateResponse:
    try:
        template = await get_template_or_raise(session, template_id)
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=template.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.ADMIN},
    )

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return SettlementTemplateResponse.model_validate(template)

    template = await update_template(session, template=template, updates=updates)
    return SettlementTemplateResponse.model_validate(template)


@router.delete(
    "/templates/{template_id}",
    response_model=SettlementTemplateResponse,
    summary="템플릿 비활성화 (Treasurer/Admin)",
)
async def deactivate_template(
    template_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementTemplateResponse:
    """Soft-delete by flipping `is_active` to False.

    We never hard-delete because settlements may FK to historical templates.
    """
    try:
        template = await get_template_or_raise(session, template_id)
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=template.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.ADMIN},
    )

    template = await update_template(
        session,
        template=template,
        updates={"is_active": False},
    )
    return SettlementTemplateResponse.model_validate(template)
