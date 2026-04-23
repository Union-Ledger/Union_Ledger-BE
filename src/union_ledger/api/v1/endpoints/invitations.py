from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user, require_roles_in_org
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationMembership, User
from union_ledger.models.enums import InvitationType, RoleType
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.invitation import (
    InvitationAcceptRequest,
    InvitationCreateRequest,
    InvitationResponse,
    RoleTransferRequest,
)
from union_ledger.services.invitation import (
    InvitationEmailMismatch,
    InvitationNotAcceptable,
    InvitationNotFound,
    MembershipAlreadyExists,
    accept_invitation,
    issue_invitation,
    issue_role_transfer,
    list_invitations,
    revoke_invitation,
)
from union_ledger.services.organization import (
    OrganizationNotFound,
    get_organization_or_raise,
)

router = APIRouter(tags=["invitations"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AdminOnly = Annotated[
    OrganizationMembership,
    Depends(require_roles_in_org(RoleType.ADMIN)),
]
CurrentRoleHolder = Annotated[
    OrganizationMembership,
    Depends(
        require_roles_in_org(RoleType.TREASURER, RoleType.AUDITOR, RoleType.ADMIN),
    ),
]


def _to_response(
    invitation,
    *,
    include_code: bool = False,
) -> InvitationResponse:
    # `model_validate` pulls `code` off the ORM row automatically via
    # `from_attributes=True`. Strip it unless the caller explicitly opted in —
    # codes are secret and MUST NOT leak through listings or accept responses.
    data = InvitationResponse.model_validate(invitation)
    if include_code:
        data = data.model_copy(update={"code": invitation.code})
    else:
        data = data.model_copy(update={"code": None})
    return data


@router.post(
    "/organizations/{organization_id}/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="재정담당자 / 감사위원 초대 코드 발급 (Admin)",
)
async def create_invitation(
    organization_id: uuid.UUID,
    payload: InvitationCreateRequest,
    _membership: AdminOnly,
    session: DbSession,
) -> InvitationResponse:
    if payload.invitation_type == InvitationType.ROLE_TRANSFER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "권한 이전은 /organizations/{id}/memberships/me/transfer "
                "엔드포인트를 사용해 주세요."
            ),
        )

    try:
        organization = await get_organization_or_raise(session, organization_id)
    except OrganizationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    issued = await issue_invitation(
        session,
        organization=organization,
        invited_email=payload.invited_email,
        invitation_type=payload.invitation_type,
        role=payload.role,
    )
    # Expose the code on first issuance so the Admin can forward it out-of-band.
    return _to_response(issued.invitation, include_code=True)


@router.get(
    "/organizations/{organization_id}/invitations",
    response_model=list[InvitationResponse],
    summary="조직 초대 목록 (Admin)",
)
async def list_org_invitations(
    organization_id: uuid.UUID,
    _membership: AdminOnly,
    session: DbSession,
) -> list[InvitationResponse]:
    items = await list_invitations(session, organization_id=organization_id)
    # Never leak codes in listings — even for Admins.
    return [_to_response(item, include_code=False) for item in items]


@router.delete(
    "/organizations/{organization_id}/invitations/{invitation_id}",
    response_model=InvitationResponse,
    summary="초대 회수 (Admin)",
)
async def revoke_org_invitation(
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    _membership: AdminOnly,
    session: DbSession,
) -> InvitationResponse:
    try:
        invitation = await revoke_invitation(
            session,
            invitation_id=invitation_id,
            organization_id=organization_id,
        )
    except InvitationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvitationNotAcceptable as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _to_response(invitation, include_code=False)


@router.post(
    "/organizations/{organization_id}/memberships/me/transfer",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="내 역할을 후임자에게 이전",
)
async def transfer_role(
    organization_id: uuid.UUID,
    payload: RoleTransferRequest,
    membership: CurrentRoleHolder,
    session: DbSession,
) -> InvitationResponse:
    try:
        organization = await get_organization_or_raise(session, organization_id)
    except OrganizationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    issued = await issue_role_transfer(
        session,
        organization=organization,
        current_membership=membership,
        successor_email=payload.successor_email,
    )
    return _to_response(issued.invitation, include_code=True)


@router.post(
    "/invitations/accept",
    response_model=InvitationResponse,
    summary="초대 코드 수락 (이미 가입된 사용자)",
)
async def accept(
    payload: InvitationAcceptRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> InvitationResponse:
    # get_current_user returns the DTO `AuthUser`. The service needs the ORM
    # `User` (it checks `user.email` and attaches a new membership row).
    user = await session.get(User, current_user.id)
    if user is None:
        # User was deleted between the JWT being issued and this request.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    try:
        result = await accept_invitation(session, code=payload.code, user=user)
    except InvitationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvitationNotAcceptable as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except InvitationEmailMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except MembershipAlreadyExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _to_response(result.invitation, include_code=False)
