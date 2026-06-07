from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from union_ledger.models.enums import AdminApplicationStatus


class AdminApplicationDocumentInfo(BaseModel):
    """Proof-document metadata returned to clients.

    The on-disk path is intentionally omitted — files are fetched through the
    document download endpoint by index, which re-checks authorization.
    """

    file_name: str
    content_type: str | None = None
    size: int = 0


class AdminApplicationResponse(BaseModel):
    id: uuid.UUID
    applicant_user_id: uuid.UUID
    applicant_name: str | None = None
    applicant_email: str | None = None
    organization_name: str
    college_name: str
    department_name: str
    documents: list[AdminApplicationDocumentInfo] = Field(default_factory=list)
    status: AdminApplicationStatus
    review_note: str | None = None
    reviewed_at: datetime | None = None
    created_organization_id: uuid.UUID | None = None
    created_at: datetime


class AdminApplicationReviewRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class AdminApplicationApproveResponse(BaseModel):
    application: AdminApplicationResponse
    organization_id: uuid.UUID
