"""Organization + membership services.

Sits on top of the schema that teammate's auth layer already populates:
  - `users` rows come from `POST /auth/signup`.
  - `organization_memberships` come from signup's auto-create path, from
    invitation acceptance, and from `POST /organizations` here.

This module intentionally does NOT touch the signup auto-create behavior —
that's teammate's territory. A user who explicitly calls `POST /organizations`
becomes ADMIN of the new org; they may also still hold other memberships
(e.g. STUDENT in the signup-time auto-created org).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from union_ledger.models.entities import Organization, OrganizationMembership, User
from union_ledger.models.enums import RoleType


class OrganizationError(Exception):
    """Base class for organization-layer errors (maps to HTTP 4xx)."""


class OrganizationNotFound(OrganizationError):
    pass


async def create_organization(
    session: AsyncSession,
    *,
    creator_id: uuid.UUID,
    name: str,
    college_name: str,
    department_name: str,
) -> Organization:
    """Create an organization and add the creator as primary Admin."""
    organization = Organization(
        name=name,
        college_name=college_name,
        department_name=department_name,
        created_by_id=creator_id,
    )
    session.add(organization)
    await session.flush()

    membership = OrganizationMembership(
        organization_id=organization.id,
        user_id=creator_id,
        role=RoleType.PRESIDENT,
        is_primary=True,
    )
    session.add(membership)

    await session.commit()
    await session.refresh(organization)
    return organization


async def list_user_organizations(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> Sequence[Organization]:
    stmt = (
        select(Organization)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(OrganizationMembership.user_id == user_id)
        .order_by(Organization.created_at.desc())
    )
    result = await session.scalars(stmt)
    # Deduplicate — a user can hold multiple roles in the same org.
    return list({org.id: org for org in result.all()}.values())


async def get_organization_or_raise(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> Organization:
    org = await session.get(Organization, organization_id)
    if org is None:
        raise OrganizationNotFound("조직을 찾을 수 없습니다.")
    return org


async def list_memberships(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> Sequence[OrganizationMembership]:
    stmt = (
        select(OrganizationMembership)
        .options(selectinload(OrganizationMembership.user))
        .where(OrganizationMembership.organization_id == organization_id)
        .order_by(OrganizationMembership.created_at.asc())
    )
    result = await session.scalars(stmt)
    return list(result.all())


# Re-export User for callers that need to touch the relationship without a
# separate import.
__all__ = [
    "OrganizationError",
    "OrganizationNotFound",
    "User",
    "create_organization",
    "get_organization_or_raise",
    "list_memberships",
    "list_user_organizations",
]
