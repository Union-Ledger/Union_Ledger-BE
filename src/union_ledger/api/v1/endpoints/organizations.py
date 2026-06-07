from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import (
    get_current_user,
    require_operator,
    require_roles_in_org,
)
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationMembership
from union_ledger.models.enums import RoleType
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.organization import (
    OrganizationCreateRequest,
    OrganizationMemberResponse,
    OrganizationResponse,
)
from union_ledger.services.organization import (
    OrganizationNotFound,
    create_organization,
    get_organization_or_raise,
    list_memberships,
    list_user_organizations,
)

router = APIRouter(tags=["organizations"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AnyMember = Annotated[OrganizationMembership, Depends(require_roles_in_org())]
AdminOnly = Annotated[
    OrganizationMembership,
    Depends(require_roles_in_org(RoleType.ADMIN)),
]


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="학생회 조직 직접 생성 (운영자 전용 / 시딩용)",
)
async def create_org(
    payload: OrganizationCreateRequest,
    operator: Annotated[AuthUser, Depends(require_operator)],
    session: DbSession,
) -> OrganizationResponse:
    # Operator-only escape hatch (the operator becomes ADMIN). The normal way a
    # 회장 gets an org is the document-reviewed application flow
    # (/admin-applications) — this exists for operator seeding/repair only.
    organization = await create_organization(
        session,
        creator_id=operator.id,
        name=payload.name,
        college_name=payload.college_name,
        department_name=payload.department_name,
    )
    return OrganizationResponse.model_validate(organization)


@router.get(
    "/organizations",
    response_model=list[OrganizationResponse],
    summary="내가 소속된 조직 목록",
)
async def list_orgs(
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> list[OrganizationResponse]:
    items = await list_user_organizations(session, user_id=current_user.id)
    return [OrganizationResponse.model_validate(item) for item in items]


@router.get(
    "/organizations/{organization_id}",
    response_model=OrganizationResponse,
    summary="조직 상세 조회 (멤버 전용)",
)
async def get_org(
    organization_id: uuid.UUID,
    _membership: AnyMember,
    session: DbSession,
) -> OrganizationResponse:
    try:
        organization = await get_organization_or_raise(session, organization_id)
    except OrganizationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return OrganizationResponse.model_validate(organization)


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[OrganizationMemberResponse],
    summary="조직 구성원 목록 (Admin 전용)",
)
async def list_org_members(
    organization_id: uuid.UUID,
    _membership: AdminOnly,
    session: DbSession,
) -> list[OrganizationMemberResponse]:
    memberships = await list_memberships(session, organization_id)
    return [OrganizationMemberResponse.model_validate(m) for m in memberships]
