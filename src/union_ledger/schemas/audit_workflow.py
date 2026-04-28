"""Auditor-side workflow schemas (spec §3).

Two main shapes:
  - `AuditWorklistItem` — one row in the auditor's settlement queue, with
    counts that the FE needs for the dashboard badge.
  - `AuditReviewBundle` — the comparison-view screen payload: settlement
    + evidences + bank transactions + reconciliation rows + comments, all
    in one response so the FE can paint the side-by-side view in one fetch.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from union_ledger.models.enums import SettlementStatus
from union_ledger.schemas.audit import AuditCommentResponse
from union_ledger.schemas.bank_statement import BankTransactionResponse
from union_ledger.schemas.evidence import EvidenceResponse
from union_ledger.schemas.reconciliation import ReconciliationResultResponse
from union_ledger.schemas.settlement import SettlementResponse


class AuditReconciliationSummary(BaseModel):
    matched: int = 0
    amount_mismatch: int = 0
    date_mismatch: int = 0
    missing_bank_transaction: int = 0
    missing_evidence: int = 0
    manually_resolved: int = 0


class AuditWorklistItem(BaseModel):
    settlement_id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    college_name: str
    department_name: str
    title: str
    academic_year: int
    semester: str
    status: SettlementStatus
    submitted_at: datetime | None
    audited_at: datetime | None
    evidence_count: int
    bank_transaction_count: int
    audit_comment_count: int
    total_evidence_amount: Decimal = Field(
        default=Decimal(0),
        description="등록된 증빙의 amount 합계 (집계는 양수 절대값 기준).",
    )
    reconciliation: AuditReconciliationSummary

    model_config = ConfigDict(from_attributes=False)


class AuditReviewBundle(BaseModel):
    """Comparison-view payload for `GET /audit/settlements/{id}`."""

    settlement: SettlementResponse
    evidences: list[EvidenceResponse]
    bank_transactions: list[BankTransactionResponse]
    reconciliation_results: list[ReconciliationResultResponse]
    comments: list[AuditCommentResponse]


class AuditCommentUpdateRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
