"""Reconciliation schemas (spec §2 Step 3–4).

A `ReconciliationResult` is a row in the dashboard the FE renders with the
green / red / yellow color codes:

  - green  → status=MATCHED            (evidence + bank_transaction both set)
  - yellow → status=AMOUNT_MISMATCH | DATE_MISMATCH | MANUALLY_RESOLVED
  - red    → status=MISSING_BANK_TRANSACTION (evidence only, no bank row)
           → status=MISSING_EVIDENCE         (bank row only, no evidence)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from union_ledger.models.enums import MatchStatus


class ReconciliationResultResponse(BaseModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    evidence_id: uuid.UUID | None
    bank_transaction_id: uuid.UUID | None
    status: MatchStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReconciliationRunResponse(BaseModel):
    """Summary of a reconciliation run, plus the full result list."""

    settlement_id: uuid.UUID
    total: int
    matched: int
    amount_mismatch: int
    date_mismatch: int
    missing_bank_transaction: int
    missing_evidence: int
    results: list[ReconciliationResultResponse]


class ReconciliationUpdateRequest(BaseModel):
    """Manual override after auto-match (spec §2 Step 4).

    All fields optional. Setting `evidence_id` / `bank_transaction_id` to
    null is allowed — useful when reverting a manually-linked pair.
    """

    evidence_id: uuid.UUID | None = None
    bank_transaction_id: uuid.UUID | None = None
    status: MatchStatus | None = None
    notes: str | None = Field(default=None, max_length=2000)
