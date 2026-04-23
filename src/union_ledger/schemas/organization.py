from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from union_ledger.models.enums import RoleType


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    college_name: str = Field(min_length=1, max_length=120)
    department_name: str = Field(min_length=1, max_length=120)


class OrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    college_name: str
    department_name: str
    created_by_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrganizationMemberUser(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None

    model_config = ConfigDict(from_attributes=True)


class OrganizationMemberResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    user: OrganizationMemberUser
    role: RoleType
    is_primary: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
