"""Notification endpoints (spec §5-2).

Routes:
  GET   /notifications                 — current user's inbox
  POST  /notifications/{id}/read       — mark a notification as read

Notifications are personal — every endpoint scopes by the caller's user_id;
trying to read someone else's notification returns 404 (we don't leak the
existence of other users' rows).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user
from union_ledger.db.session import get_db_session
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.notification import (
    NotificationListResponse,
    NotificationResponse,
)
from union_ledger.services.notification import (
    NotificationNotFound,
    count_user_notifications,
    list_user_notifications,
    mark_notification_read,
)

router = APIRouter(tags=["notifications"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/notifications",
    response_model=NotificationListResponse,
    summary="내 알림 목록 (총건수 + 미읽음수)",
)
async def list_my_notifications(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    only_unread: Annotated[
        bool,
        Query(description="true 면 미읽음만 반환"),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NotificationListResponse:
    items = await list_user_notifications(
        session,
        user_id=current_user.id,
        only_unread=only_unread,
        limit=limit,
    )
    total, unread = await count_user_notifications(session, user_id=current_user.id)
    return NotificationListResponse(
        total=total,
        unread=unread,
        items=[NotificationResponse.model_validate(n) for n in items],
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationResponse,
    summary="알림 읽음 처리",
)
async def mark_read(
    notification_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> NotificationResponse:
    try:
        notification = await mark_notification_read(
            session,
            notification_id=notification_id,
            user_id=current_user.id,
        )
    except NotificationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return NotificationResponse.model_validate(notification)
