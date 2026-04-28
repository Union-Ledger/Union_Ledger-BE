"""Notification service (spec §5-2).

This module provides:
  - CRUD-ish helpers for the per-user inbox: list, mark-read.
  - A `fanout_to_org_roles` helper that creates one notification row per
    distinct user holding any of the requested roles in the org.
  - Higher-level `notify_*` helpers wired to the 5 settlement lifecycle
    events: submit, resubmit, approve, reject, publish.

Design notes:
  - We dedupe by user when fanning out — a user with multiple roles in the
    same org should still receive a single notification.
  - Notifications are the LAST thing we emit in a transaction. Callers
    pass an already-flushed/committed Settlement; this module does its own
    `session.commit()` so a notification failure cannot roll back the
    underlying lifecycle change.
  - We never raise on notify failure — at most we log via the exception
    type. The lifecycle change has higher business value than the
    notification.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    Notification,
    Organization,
    OrganizationMembership,
    Settlement,
)
from union_ledger.models.enums import NotificationType, RoleType


class NotificationError(Exception):
    """Base notification error."""


class NotificationNotFound(NotificationError):
    pass


# --- Inbox queries -------------------------------------------------------


async def list_user_notifications(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    only_unread: bool = False,
    limit: int = 50,
) -> Sequence[Notification]:
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    if only_unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    result = await session.scalars(stmt)
    return list(result.all())


async def count_user_notifications(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> tuple[int, int]:
    """Returns (total, unread)."""
    total = await session.scalar(
        select(func.count(Notification.id)).where(Notification.user_id == user_id)
    )
    unread = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
    return int(total or 0), int(unread or 0)


async def mark_notification_read(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Notification:
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        # Treat "not yours" as "not found" — never disclose existence to
        # other users.
        raise NotificationNotFound("알림을 찾을 수 없습니다.")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(notification)
    return notification


# --- Fanout --------------------------------------------------------------


async def fanout_to_org_roles(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    roles: Iterable[RoleType],
    notification_type: NotificationType,
    title: str,
    body: str,
    exclude_user_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Create one notification per distinct user holding any of `roles`.

    `exclude_user_id` lets the caller skip the user who triggered the event
    (e.g., we don't notify the auditor of their own approve action).
    """
    role_set = set(roles)
    if not role_set:
        return []

    user_ids = await session.scalars(
        select(OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role.in_(role_set),
        )
        .distinct()
    )
    distinct_user_ids = {uid for uid in user_ids.all() if uid != exclude_user_id}
    if not distinct_user_ids:
        return []

    rows = [
        Notification(
            user_id=uid,
            notification_type=notification_type,
            title=title,
            body=body,
        )
        for uid in distinct_user_ids
    ]
    for row in rows:
        session.add(row)
    await session.commit()
    for row in rows:
        await session.refresh(row)
    return rows


# --- Lifecycle helpers ---------------------------------------------------


async def _settlement_label(session: AsyncSession, settlement: Settlement) -> str:
    """Human-readable settlement label — "<org name> <year>-<sem>학기 · <title>"."""
    org = await session.get(Organization, settlement.organization_id)
    org_name = org.name if org is not None else ""
    period = f"{settlement.academic_year}-{settlement.semester}학기"
    return f"{org_name} {period} · {settlement.title}".strip()


async def notify_settlement_submitted(
    session: AsyncSession,
    *,
    settlement: Settlement,
    actor_user_id: uuid.UUID | None = None,
) -> list[Notification]:
    label = await _settlement_label(session, settlement)
    return await fanout_to_org_roles(
        session,
        organization_id=settlement.organization_id,
        roles={RoleType.AUDITOR},
        notification_type=NotificationType.SETTLEMENT_SUBMITTED,
        title="결산안이 제출되었습니다",
        body=f"{label} 의 감사가 요청되었습니다.",
        exclude_user_id=actor_user_id,
    )


async def notify_settlement_resubmitted(
    session: AsyncSession,
    *,
    settlement: Settlement,
    actor_user_id: uuid.UUID | None = None,
) -> list[Notification]:
    label = await _settlement_label(session, settlement)
    return await fanout_to_org_roles(
        session,
        organization_id=settlement.organization_id,
        roles={RoleType.AUDITOR},
        notification_type=NotificationType.SETTLEMENT_RESUBMITTED,
        title="수정된 결산안이 재제출되었습니다",
        body=f"{label} 의 재감사가 요청되었습니다.",
        exclude_user_id=actor_user_id,
    )


async def notify_audit_approved(
    session: AsyncSession,
    *,
    settlement: Settlement,
    actor_user_id: uuid.UUID | None = None,
) -> list[Notification]:
    label = await _settlement_label(session, settlement)
    return await fanout_to_org_roles(
        session,
        organization_id=settlement.organization_id,
        roles={RoleType.TREASURER, RoleType.ADMIN},
        notification_type=NotificationType.AUDIT_APPROVED,
        title="결산안이 승인되었습니다",
        body=f"{label} 의 감사가 승인되었습니다.",
        exclude_user_id=actor_user_id,
    )


async def notify_audit_rejected(
    session: AsyncSession,
    *,
    settlement: Settlement,
    actor_user_id: uuid.UUID | None = None,
) -> list[Notification]:
    label = await _settlement_label(session, settlement)
    return await fanout_to_org_roles(
        session,
        organization_id=settlement.organization_id,
        roles={RoleType.TREASURER, RoleType.ADMIN},
        notification_type=NotificationType.AUDIT_REJECTED,
        title="결산안이 반려되었습니다",
        body=f"{label} 가 반려되었습니다. 감사 코멘트를 확인해주세요.",
        exclude_user_id=actor_user_id,
    )


async def notify_settlement_published(
    session: AsyncSession,
    *,
    settlement: Settlement,
    actor_user_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Spec §5-2: notify '일반 학우' on publish.

    The spec's '일반 학우' phrasing maps to "anyone affiliated with the org".
    Since there's no public-student concept yet (every user gets STUDENT in
    their auto-created signup org, never in someone else's org), we fan out
    to every member of the published org regardless of role. Treasurers /
    Auditors / Admins who are part of the org also get the heads-up.
    """
    label = await _settlement_label(session, settlement)
    return await fanout_to_org_roles(
        session,
        organization_id=settlement.organization_id,
        roles={RoleType.STUDENT, RoleType.TREASURER, RoleType.AUDITOR, RoleType.ADMIN},
        notification_type=NotificationType.SETTLEMENT_PUBLISHED,
        title="결산안이 공개되었습니다",
        body=f"{label} 가 공개되었습니다. 자세한 내용을 확인해보세요.",
        exclude_user_id=actor_user_id,
    )
