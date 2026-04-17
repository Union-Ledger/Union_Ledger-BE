from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.security import decode_access_token
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import OrganizationMembership, User
from union_ledger.models.enums import RoleType
from union_ledger.schemas.auth_response import AuthUser

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AuthUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise credentials_exception

    try:
        payload = decode_access_token(credentials.credentials)
        subject = payload.get("sub")
        if subject is None:
            raise credentials_exception
        user_id = uuid.UUID(subject)
    except (ValueError, TypeError):
        raise credentials_exception

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_exception

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
    return RoleChecker(allowed_roles)
