"""Invitation services — issue, list, revoke, accept, role transfers.

The teammate's signup endpoint already consumes an `invitation_code` as part
of account creation (path A). This module adds path B: an already-registered
user accepts an invitation after the fact via `POST /invitations/accept`.
Both paths mark the `Invitation` row as ACCEPTED.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    Invitation,
    Organization,
    OrganizationMembership,
    User,
)
from union_ledger.models.enums import InvitationStatus, InvitationType, RoleType

# Keep this a plain constant for now — teammate's Settings doesn't expose it
# and we don't want to churn their config surface on this PR.
INVITATION_EXPIRES_DAYS = 7


class InvitationError(Exception):
    """Base class for invitation-layer errors (maps to HTTP 4xx)."""


class InvitationNotFound(InvitationError):
    pass


class InvitationNotAcceptable(InvitationError):
    pass


class InvitationEmailMismatch(InvitationError):
    pass


class MembershipAlreadyExists(InvitationError):
    pass


@dataclass
class IssuedInvitation:
    invitation: Invitation
    plain_code: str


@dataclass
class AcceptedInvitation:
    invitation: Invitation
    membership: OrganizationMembership


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _generate_code(nbytes: int = 18) -> str:
    """URL-safe random code (~24 chars)."""
    return secrets.token_urlsafe(nbytes)


async def issue_invitation(
    session: AsyncSession,
    *,
    organization: Organization,
    invited_email: str,
    invitation_type: InvitationType,
    role: RoleType,
) -> IssuedInvitation:
    code = _generate_code()
    expires_at = _now() + timedelta(days=INVITATION_EXPIRES_DAYS)
    invitation = Invitation(
        organization_id=organization.id,
        invited_email=_normalize_email(invited_email),
        invitation_type=invitation_type,
        role=role,
        code=code,
        status=InvitationStatus.PENDING,
        expires_at=expires_at,
    )
    session.add(invitation)
    await session.commit()
    await session.refresh(invitation)
    return IssuedInvitation(invitation=invitation, plain_code=code)


async def list_invitations(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
) -> list[Invitation]:
    stmt = (
        select(Invitation)
        .where(Invitation.organization_id == organization_id)
        .order_by(Invitation.created_at.desc())
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def list_received_invitations(
    session: AsyncSession,
    *,
    email: str,
    pending_only: bool = True,
) -> list[Invitation]:
    """Invitations addressed to ``email`` (i.e. the ones a user *received*).

    Defaults to PENDING only so the caller can render a "받은 초대" inbox that
    only shows actionable items.
    """
    stmt = select(Invitation).where(
        Invitation.invited_email == _normalize_email(email)
    )
    if pending_only:
        stmt = stmt.where(Invitation.status == InvitationStatus.PENDING)
    stmt = stmt.order_by(Invitation.created_at.desc())
    result = await session.scalars(stmt)
    return list(result.all())


async def revoke_invitation(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> Invitation:
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != organization_id:
        raise InvitationNotFound("초대를 찾을 수 없습니다.")
    if invitation.status != InvitationStatus.PENDING:
        raise InvitationNotAcceptable("pending 상태의 초대만 회수할 수 있습니다.")
    invitation.status = InvitationStatus.REVOKED
    await session.commit()
    await session.refresh(invitation)
    return invitation


async def accept_invitation(
    session: AsyncSession,
    *,
    code: str,
    user: User,
) -> AcceptedInvitation:
    """Accept by secret code (signup/legacy path)."""
    invitation = await session.scalar(select(Invitation).where(Invitation.code == code))
    if invitation is None:
        raise InvitationNotFound("유효하지 않은 초대 코드입니다.")
    return await _accept_loaded_invitation(session, invitation=invitation, user=user)


async def accept_invitation_by_id(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    user: User,
) -> AcceptedInvitation:
    """Accept by invitation id (in-app path — the invitee taps 수락 in their
    received-invitations list; no secret code needs to be passed around)."""
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None:
        raise InvitationNotFound("초대를 찾을 수 없습니다.")
    return await _accept_loaded_invitation(session, invitation=invitation, user=user)


async def _accept_loaded_invitation(
    session: AsyncSession,
    *,
    invitation: Invitation,
    user: User,
) -> AcceptedInvitation:
    if invitation.status != InvitationStatus.PENDING:
        raise InvitationNotAcceptable("이미 처리되었거나 만료된 초대입니다.")

    expires_at = invitation.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < _now():
            invitation.status = InvitationStatus.EXPIRED
            await session.commit()
            raise InvitationNotAcceptable("만료된 초대입니다.")

    if invitation.invited_email.lower() != user.email.lower():
        raise InvitationEmailMismatch(
            "초대된 이메일과 현재 로그인된 계정이 일치하지 않습니다."
        )

    # Duplicate membership guard (same org + user + role).
    existing = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == invitation.organization_id,
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.role == invitation.role,
        )
    )
    if existing is not None:
        raise MembershipAlreadyExists("이미 해당 역할로 소속된 조직입니다.")

    if invitation.invitation_type == InvitationType.ROLE_TRANSFER:
        # Hand-off: remove prior holders of this role in the organization.
        prior_stmt = select(OrganizationMembership).where(
            OrganizationMembership.organization_id == invitation.organization_id,
            OrganizationMembership.role == invitation.role,
        )
        prior_rows = (await session.scalars(prior_stmt)).all()
        for row in prior_rows:
            await session.delete(row)

    membership = OrganizationMembership(
        organization_id=invitation.organization_id,
        user_id=user.id,
        role=invitation.role,
        is_primary=(invitation.invitation_type == InvitationType.ROLE_TRANSFER),
    )
    session.add(membership)
    invitation.status = InvitationStatus.ACCEPTED
    await session.commit()
    await session.refresh(membership)
    await session.refresh(invitation)
    return AcceptedInvitation(invitation=invitation, membership=membership)


async def decline_invitation(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    user: User,
) -> Invitation:
    """The invited user declines a pending invitation they received.

    Mirrors ``accept_invitation`` on the rejection side: the caller must be
    logged in as the invited email, and only PENDING invitations can be
    declined (revoked/accepted/expired ones are terminal).
    """
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None:
        raise InvitationNotFound("초대를 찾을 수 없습니다.")
    if invitation.invited_email.lower() != user.email.lower():
        raise InvitationEmailMismatch(
            "초대된 이메일과 현재 로그인된 계정이 일치하지 않습니다."
        )
    if invitation.status != InvitationStatus.PENDING:
        raise InvitationNotAcceptable("이미 처리되었거나 만료된 초대입니다.")

    invitation.status = InvitationStatus.DECLINED
    await session.commit()
    await session.refresh(invitation)
    return invitation


async def issue_role_transfer(
    session: AsyncSession,
    *,
    organization: Organization,
    current_membership: OrganizationMembership,
    successor_email: str,
) -> IssuedInvitation:
    """Hand off the caller's role to `successor_email`.

    The successor must sign up / log in with that email, then POST
    `/invitations/accept` with the returned code.
    """
    return await issue_invitation(
        session,
        organization=organization,
        invited_email=successor_email,
        invitation_type=InvitationType.ROLE_TRANSFER,
        role=current_membership.role,
    )
