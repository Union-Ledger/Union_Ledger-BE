from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.config import get_settings
from union_ledger.core.security import decode_access_token
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationMembership, User
from union_ledger.models.enums import RoleType
from union_ledger.schemas.auth_response import AuthUser

bearer_scheme = HTTPBearer(auto_error=False)


def _credentials_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _resolve_user_id(credentials: HTTPAuthorizationCredentials | None) -> uuid.UUID:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _credentials_error()
    try:
        payload = decode_access_token(credentials.credentials)
        subject = payload.get("sub")
        if subject is None:
            raise _credentials_error()
        return uuid.UUID(subject)
    except (ValueError, TypeError) as exc:
        raise _credentials_error() from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AuthUser:
    user_id = await _resolve_user_id(credentials)

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise _credentials_error()

    roles_result = await session.scalars(
        select(OrganizationMembership.role).where(OrganizationMembership.user_id == user.id)
    )
    roles = sorted(set(roles_result.all()), key=lambda value: value.value)

    return AuthUser(
        id=user.id,
        email=user.email,
        name=user.name,
        roles=roles,
    )


def require_operator(
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> AuthUser:
    """Platform-operator gate.

    Operators are the maintaining developers, identified by the
    ``OPERATOR_EMAILS`` allowlist (see ``Settings``). They review 회장(admin)
    applications and may create organizations directly. This is a
    platform-level capability, independent of any org membership.
    """
    operator_emails = {email.lower() for email in get_settings().operator_emails}
    if not operator_emails or current_user.email.lower() not in operator_emails:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="운영자 권한이 필요합니다.",
        )
    return current_user


class RoleChecker:
    def __init__(self, allowed_roles: Iterable[RoleType]) -> None:
        self.allowed_roles = set(allowed_roles)

    async def __call__(self, current_user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if not self.allowed_roles.intersection(set(current_user.roles)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="해당 기능에 접근할 권한이 없습니다.",
            )
        return current_user


def require_roles(*allowed_roles: RoleType) -> RoleChecker:
    """Global role gate. A user with role R in *any* organization passes.

    Use this only for org-agnostic actions. For anything scoped to a specific
    organization (members list, invitations, settlements), use
    `require_roles_in_org` — otherwise an Admin of org A would slip through
    gates on org B's resources.
    """
    return RoleChecker(allowed_roles)


async def require_membership_for_org(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    allowed_roles: set[RoleType] | None = None,
) -> OrganizationMembership:
    """Inline org-membership guard for routes that don't carry org_id in the path.

    Use this from item-level endpoints (e.g. `/settlements/{id}`) where the
    org id has to be resolved from the loaded resource rather than the URL.
    For collection routes that *do* have `{organization_id}` in the path, use
    the `require_roles_in_org` dependency factory instead.

    Pass `allowed_roles=None` (default) to accept any membership; pass a set
    of `RoleType` to additionally require a specific role.
    """
    memberships = (
        await session.scalars(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
            )
        )
    ).all()
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="조직 접근 권한이 없습니다.",
        )
    if not allowed_roles:
        return memberships[0]
    for membership in memberships:
        if membership.role in allowed_roles:
            return membership
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="이 작업을 수행할 역할이 없습니다.",
    )


def require_roles_in_org(
    *allowed_roles: RoleType,
) -> Callable[..., OrganizationMembership]:
    """Organization-scoped role gate.

    Uses the `organization_id` path parameter to look up the caller's
    membership in *that* organization, and requires the membership's role to
    match one of `allowed_roles` (or, if `allowed_roles` is empty, any
    membership). Returns the matching `OrganizationMembership` so callers can
    reuse it (e.g. role transfers need the caller's current role).
    """
    allowed_set = set(allowed_roles)

    async def _checker(
        organization_id: uuid.UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        session: AsyncSession = Depends(get_db_session),
    ) -> OrganizationMembership:
        user_id = await _resolve_user_id(credentials)
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            raise _credentials_error()

        memberships = (
            await session.scalars(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == user_id,
                )
            )
        ).all()
        if not memberships:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="조직 접근 권한이 없습니다.",
            )
        for membership in memberships:
            if not allowed_set or membership.role in allowed_set:
                return membership
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="이 작업을 수행할 역할이 없습니다.",
        )

    return _checker
