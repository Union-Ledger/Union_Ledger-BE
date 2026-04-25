from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditDecisionRequest(BaseModel):
    """Used for approve/reject/resubmit. `comment` is optional; if provided it
    is auto-attached as an AuditComment so the FE doesn't need a separate POST.
    """

    comment: str | None = Field(default=None, min_length=1, max_length=4000)


class AuditCommentCreateRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
    evidence_id: uuid.UUID | None = Field(
        default=None,
        description="특정 증빙에 대한 코멘트라면 해당 증빙 ID. 전체 정산에 대한 코멘트면 null.",
    )


class AuditCommentResponse(BaseModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    evidence_id: uuid.UUID | None
    author_membership_id: uuid.UUID | None
    comment: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
