"""Notification schemas (spec §5-2).

In-app notifications fire on five settlement lifecycle events:
  - submit     → notify org Auditors
  - resubmit   → notify org Auditors
  - approve    → notify org Treasurers/Admins
  - reject     → notify org Treasurers/Admins
  - publish    → notify all org Students
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from union_ledger.models.enums import NotificationType


class NotificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    notification_type: NotificationType
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationListResponse(BaseModel):
    """List wrapper that also exposes the unread count for badge UIs."""

    total: int
    unread: int
    items: list[NotificationResponse]
