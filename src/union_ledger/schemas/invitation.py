from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from union_ledger.models.enums import InvitationStatus, InvitationType, RoleType


class InvitationCreateRequest(BaseModel):
    invitation_type: InvitationType
    invited_email: str = Field(min_length=3, max_length=255)
    role: RoleType

    @model_validator(mode="after")
    def _type_role_consistency(self) -> Self:
        expected: dict[InvitationType, RoleType] = {
            InvitationType.TREASURER_INVITE: RoleType.TREASURER,
            InvitationType.AUDITOR_INVITE: RoleType.AUDITOR,
        }
        required_role = expected.get(self.invitation_type)
        if required_role is not None and self.role != required_role:
            raise ValueError(
                f"invitation_type={self.invitation_type.value} 에는 "
                f"role={required_role.value} 만 허용됩니다."
            )
        if self.invitation_type == InvitationType.ROLE_TRANSFER and self.role not in {
            RoleType.TREASURER,
            RoleType.AUDITOR,
            RoleType.PRESIDENT,
        }:
            raise ValueError("권한 이전은 treasurer/auditor/admin 만 지원됩니다.")
        return self


class InvitationResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    invited_email: str
    invitation_type: InvitationType
    role: RoleType
    status: InvitationStatus
    expires_at: datetime | None
    created_at: datetime
    code: str | None = Field(
        default=None,
        description=(
            "발급 직후 응답에만 포함되는 초대 코드. "
            "이후 조회·수락 응답에는 보안을 위해 제외됩니다."
        ),
    )

    model_config = ConfigDict(from_attributes=True)


class ReceivedInvitationResponse(BaseModel):
    """Invitee-facing view of a pending invitation. The invitee isn't a member
    yet (so can't fetch the org separately) — carry the org identity inline.
    The secret code is never exposed; acceptance is by invitation id."""

    id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str | None = None
    college_name: str | None = None
    department_name: str | None = None
    invitation_type: InvitationType
    role: RoleType
    status: InvitationStatus
    expires_at: datetime | None
    created_at: datetime


class InvitationAcceptRequest(BaseModel):
    code: str = Field(min_length=4, max_length=128)


class RoleTransferRequest(BaseModel):
    successor_email: str = Field(min_length=3, max_length=255)
