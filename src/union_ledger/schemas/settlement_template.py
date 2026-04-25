from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SettlementTemplateResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    original_filename: str
    mapping_schema: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SettlementTemplateUpdateRequest(BaseModel):
    """Used for renaming or deactivating a template.

    `mapping_schema` is editable post-upload because spec ④ allows the treasurer
    to refine cell mappings before any settlements consume the template.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    mapping_schema: dict[str, Any] | None = None
    is_active: bool | None = None
