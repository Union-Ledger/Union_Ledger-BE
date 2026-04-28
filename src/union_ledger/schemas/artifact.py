"""Settlement artifact schemas (spec §2 Step 5).

Two artifacts are produced together:
  - SETTLEMENT_EXCEL: the org's template.xlsx with cells filled per the
    template's `mapping_schema`.
  - EVIDENCE_PDF: every evidence (image + PDF) merged in date order.

The `POST :generate` endpoint deletes prior artifacts for the settlement and
creates a fresh pair, so callers always read the latest from `GET .../artifacts`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from union_ledger.models.enums import ArtifactStatus, ArtifactType


class SettlementArtifactResponse(BaseModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    artifact_type: ArtifactType
    status: ArtifactStatus
    file_path: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ArtifactGenerationResponse(BaseModel):
    """Result of `POST .../artifacts:generate` — both artifacts in one shot."""

    excel: SettlementArtifactResponse
    pdf: SettlementArtifactResponse
