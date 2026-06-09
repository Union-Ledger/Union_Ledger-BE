"""Reconciliation service (spec §2 Step 3–4).

Auto-match algorithm:
  1. Wipe any existing reconciliation rows for the settlement (idempotent re-run).
  2. Pull all confirmed/needs-review evidences (skip FAILED / EXTRACTING).
  3. Pull all bank transactions for the settlement.
  4. For each evidence, find an unmatched bank transaction with the same
     `evidence_date` and absolute-equal `amount`. Bank withdrawals carry
     negative signs (see bank_statement.py); evidence amounts are positive
     expense values, so we compare on absolute value.
  5. Anything matched → status=MATCHED.
  6. Unmatched evidence with same amount but different date → DATE_MISMATCH.
     Unmatched evidence with same date but different amount → AMOUNT_MISMATCH.
  7. Otherwise the leftover evidence → MISSING_BANK_TRANSACTION.
  8. Leftover bank transactions → MISSING_EVIDENCE.

Tie-breaker for many-to-many same-date/same-amount (spec §9 outstanding
question): we pair them in insertion order. This is stable and predictable;
auditor can re-pair manually if needed.

The service does NOT delete bank transactions or evidences — it only writes
ReconciliationResult rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    BankStatementUpload,
    BankTransaction,
    Evidence,
    ReconciliationResult,
)
from union_ledger.models.enums import EvidenceStatus, MatchStatus
from union_ledger.schemas.reconciliation import ReconciliationResultResponse


class ReconciliationError(Exception):
    """Base error (maps to HTTP 4xx)."""


class ReconciliationResultNotFound(ReconciliationError):
    pass


class ReconciliationResultMismatch(ReconciliationError):
    """Linked evidence/bank_transaction belongs to a different settlement."""


@dataclass(slots=True)
class ReconciliationSummary:
    settlement_id: uuid.UUID
    total: int
    matched: int
    amount_mismatch: int
    date_mismatch: int
    missing_bank_transaction: int
    missing_evidence: int
    manually_resolved: int
    results: list[ReconciliationResult]


# Evidence statuses worth reconciling. FAILED / EXTRACTING are noise.
_RECONCILE_EVIDENCE_STATUSES = {
    EvidenceStatus.NEEDS_REVIEW,
    EvidenceStatus.CONFIRMED,
    EvidenceStatus.UPLOADED,
}

# When exact date+amount match fails, we optionally pair by date only and flag
# AMOUNT_MISMATCH — but only if the closest same-day bank row is plausibly
# related (e.g. OCR typo 3700 vs bank 4000). Pairing 3700 with 300000 on the
# same calendar day is misleading, so skip soft match and leave both as missing.
_DATE_SOFT_MATCH_MAX_RATIO = Decimal("10")
_DATE_SOFT_MATCH_MIN_ABS_GAP = Decimal("1000")


async def run_reconciliation(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
) -> ReconciliationSummary:
    # Idempotent: drop prior results so re-running gives a clean slate.
    await session.execute(
        delete(ReconciliationResult).where(
            ReconciliationResult.settlement_id == settlement_id
        )
    )

    evidences = await _load_reconcilable_evidences(session, settlement_id)
    bank_txs = await _load_bank_transactions(session, settlement_id)

    matched: list[ReconciliationResult] = []
    amount_mismatch: list[ReconciliationResult] = []
    date_mismatch: list[ReconciliationResult] = []
    missing_bank: list[ReconciliationResult] = []

    available_bank: list[BankTransaction] = list(bank_txs)

    for evidence in evidences:
        if evidence.amount is None or evidence.evidence_date is None:
            # Without both fields we can't auto-match; flag as needing a bank tx.
            missing_bank.append(
                _make_result(
                    settlement_id=settlement_id,
                    evidence_id=evidence.id,
                    bank_transaction_id=None,
                    status=MatchStatus.MISSING_BANK_TRANSACTION,
                    notes="증빙에 날짜 또는 금액이 비어 있습니다.",
                )
            )
            continue

        target = _find_bank_match(evidence, available_bank)
        if target is not None:
            available_bank.remove(target)
            matched.append(
                _make_result(
                    settlement_id=settlement_id,
                    evidence_id=evidence.id,
                    bank_transaction_id=target.id,
                    status=MatchStatus.MATCHED,
                    notes=None,
                )
            )
            continue

        # Soft matches — same date or same amount, but not both.
        date_match = _find_partial(
            evidence, available_bank, by="date"
        )
        if date_match is not None:
            available_bank.remove(date_match)
            amount_mismatch.append(
                _make_result(
                    settlement_id=settlement_id,
                    evidence_id=evidence.id,
                    bank_transaction_id=date_match.id,
                    status=MatchStatus.AMOUNT_MISMATCH,
                    notes=(
                        f"날짜는 같으나 금액이 다릅니다. "
                        f"증빙={evidence.amount}, 거래={date_match.amount}"
                    ),
                )
            )
            continue

        amount_match = _find_partial(
            evidence, available_bank, by="amount"
        )
        if amount_match is not None:
            available_bank.remove(amount_match)
            date_mismatch.append(
                _make_result(
                    settlement_id=settlement_id,
                    evidence_id=evidence.id,
                    bank_transaction_id=amount_match.id,
                    status=MatchStatus.DATE_MISMATCH,
                    notes=(
                        f"금액은 같으나 날짜가 다릅니다. "
                        f"증빙={evidence.evidence_date}, 거래={amount_match.transaction_date}"
                    ),
                )
            )
            continue

        missing_bank.append(
            _make_result(
                settlement_id=settlement_id,
                evidence_id=evidence.id,
                bank_transaction_id=None,
                status=MatchStatus.MISSING_BANK_TRANSACTION,
                notes=None,
            )
        )

    missing_evidence: list[ReconciliationResult] = [
        _make_result(
            settlement_id=settlement_id,
            evidence_id=None,
            bank_transaction_id=tx.id,
            status=MatchStatus.MISSING_EVIDENCE,
            notes=None,
        )
        for tx in available_bank
    ]

    all_results = matched + amount_mismatch + date_mismatch + missing_bank + missing_evidence
    for row in all_results:
        session.add(row)

    await session.commit()
    for row in all_results:
        await session.refresh(row)

    return ReconciliationSummary(
        settlement_id=settlement_id,
        total=len(all_results),
        matched=len(matched),
        amount_mismatch=len(amount_mismatch),
        date_mismatch=len(date_mismatch),
        missing_bank_transaction=len(missing_bank),
        missing_evidence=len(missing_evidence),
        # A fresh run only emits the auto statuses; manual resolutions are
        # applied later via PATCH. Counted generically so the summary stays
        # correct if this ever runs over pre-resolved rows.
        manually_resolved=sum(
            1 for row in all_results if row.status == MatchStatus.MANUALLY_RESOLVED
        ),
        results=all_results,
    )


async def build_result_responses(
    session: AsyncSession,
    results: Sequence[ReconciliationResult],
) -> list[ReconciliationResultResponse]:
    """Attach evidence/bank merchant labels for API responses."""
    if not results:
        return []

    evidence_ids = {row.evidence_id for row in results if row.evidence_id is not None}
    bank_ids = {
        row.bank_transaction_id for row in results if row.bank_transaction_id is not None
    }

    evidence_names: dict[uuid.UUID, str | None] = {}
    if evidence_ids:
        evidence_rows = await session.scalars(
            select(Evidence).where(Evidence.id.in_(evidence_ids))
        )
        evidence_names = {
            evidence.id: evidence.merchant_name for evidence in evidence_rows.all()
        }

    bank_names: dict[uuid.UUID, str] = {}
    if bank_ids:
        bank_rows = await session.scalars(
            select(BankTransaction).where(BankTransaction.id.in_(bank_ids))
        )
        bank_names = {tx.id: tx.description for tx in bank_rows.all()}

    return [
        ReconciliationResultResponse.from_result(
            row,
            evidence_merchant_name=(
                evidence_names.get(row.evidence_id) if row.evidence_id else None
            ),
            bank_merchant_name=(
                bank_names.get(row.bank_transaction_id)
                if row.bank_transaction_id
                else None
            ),
        )
        for row in results
    ]


async def list_results(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
    status_filter: MatchStatus | None = None,
) -> Sequence[ReconciliationResult]:
    stmt = (
        select(ReconciliationResult)
        .where(ReconciliationResult.settlement_id == settlement_id)
        .order_by(ReconciliationResult.created_at.asc())
    )
    if status_filter is not None:
        stmt = stmt.where(ReconciliationResult.status == status_filter)
    result = await session.scalars(stmt)
    return list(result.all())


async def get_result_or_raise(
    session: AsyncSession, match_id: uuid.UUID
) -> ReconciliationResult:
    row = await session.get(ReconciliationResult, match_id)
    if row is None:
        raise ReconciliationResultNotFound("매칭 결과를 찾을 수 없습니다.")
    return row


async def update_result(
    session: AsyncSession,
    *,
    result: ReconciliationResult,
    updates: dict[str, Any],
) -> ReconciliationResult:
    """Manual override (spec §2 Step 4).

    The endpoint passes only the fields the schema permits. We additionally
    enforce that a linked evidence/bank_transaction belongs to the same
    settlement — otherwise the FE could attach unrelated rows.
    """
    if "evidence_id" in updates and updates["evidence_id"] is not None:
        evidence = await session.get(Evidence, updates["evidence_id"])
        if evidence is None or evidence.settlement_id != result.settlement_id:
            raise ReconciliationResultMismatch(
                "이 결산안에 속하지 않은 증빙입니다."
            )

    if (
        "bank_transaction_id" in updates
        and updates["bank_transaction_id"] is not None
    ):
        tx = await session.get(BankTransaction, updates["bank_transaction_id"])
        if tx is None:
            raise ReconciliationResultMismatch("거래내역을 찾을 수 없습니다.")
        upload = await session.get(BankStatementUpload, tx.upload_id)
        if upload is None or upload.settlement_id != result.settlement_id:
            raise ReconciliationResultMismatch(
                "이 결산안에 속하지 않은 거래내역입니다."
            )

    for field, value in updates.items():
        setattr(result, field, value)

    await session.commit()
    await session.refresh(result)
    return result


# --- Internals ------------------------------------------------------------


def _make_result(
    *,
    settlement_id: uuid.UUID,
    evidence_id: uuid.UUID | None,
    bank_transaction_id: uuid.UUID | None,
    status: MatchStatus,
    notes: str | None,
) -> ReconciliationResult:
    return ReconciliationResult(
        settlement_id=settlement_id,
        evidence_id=evidence_id,
        bank_transaction_id=bank_transaction_id,
        status=status,
        notes=notes,
    )


def _find_bank_match(
    evidence: Evidence, candidates: Sequence[BankTransaction]
) -> BankTransaction | None:
    """Exact match: same date AND same absolute amount."""
    target_amount = abs(evidence.amount) if evidence.amount is not None else None
    for tx in candidates:
        if (
            tx.transaction_date == evidence.evidence_date
            and target_amount is not None
            and abs(tx.amount) == target_amount
        ):
            return tx
    return None


def _find_partial(
    evidence: Evidence,
    candidates: Sequence[BankTransaction],
    *,
    by: str,
) -> BankTransaction | None:
    """Soft match by single attribute. `by` ∈ {"date", "amount"}."""
    if by == "date":
        same_day = [
            tx
            for tx in candidates
            if tx.transaction_date == evidence.evidence_date
        ]
        if not same_day:
            return None
        if evidence.amount is None or evidence.amount == _ZERO:
            return same_day[0]
        target = abs(evidence.amount)
        closest = min(same_day, key=lambda tx: abs(abs(tx.amount) - target))
        gap = abs(abs(closest.amount) - target)
        if gap <= max(target * _DATE_SOFT_MATCH_MAX_RATIO, _DATE_SOFT_MATCH_MIN_ABS_GAP):
            return closest
        return None
    elif by == "amount":
        target = abs(evidence.amount) if evidence.amount is not None else None
        for tx in candidates:
            if target is not None and abs(tx.amount) == target:
                return tx
    return None


async def _load_reconcilable_evidences(
    session: AsyncSession, settlement_id: uuid.UUID
) -> Sequence[Evidence]:
    stmt = (
        select(Evidence)
        .where(
            Evidence.settlement_id == settlement_id,
            Evidence.status.in_(_RECONCILE_EVIDENCE_STATUSES),
        )
        .order_by(Evidence.created_at.asc())
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def _load_bank_transactions(
    session: AsyncSession, settlement_id: uuid.UUID
) -> Sequence[BankTransaction]:
    stmt = (
        select(BankTransaction)
        .join(BankStatementUpload, BankTransaction.upload_id == BankStatementUpload.id)
        .where(BankStatementUpload.settlement_id == settlement_id)
        .order_by(BankTransaction.created_at.asc())
    )
    result = await session.scalars(stmt)
    return list(result.all())


# Decimal abs helper (the literal `abs()` works on Decimal already, this is
# kept here only as a type hint for readers).
_ZERO = Decimal(0)
