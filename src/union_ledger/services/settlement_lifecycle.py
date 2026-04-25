"""Settlement state-machine transitions (spec ⑧).

The Settlement object moves through these states:

    DRAFT
      └── submit ──► SUBMITTED
                       ├── approve ──► APPROVED
                       │                  └── publish ──► (sets published_at)
                       └── reject  ──► REJECTED
                                          └── resubmit ──► RESUBMITTED
                                                              ├── approve ──► APPROVED
                                                              └── reject  ──► REJECTED

Side-effect timestamps are written here, never in the endpoint:
  - `submitted_at` set on submit / resubmit
  - `audited_at`  set on approve / reject; cleared on resubmit
  - `published_at` set on publish
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import Settlement
from union_ledger.models.enums import SettlementStatus
from union_ledger.services.settlement import SettlementError


class IllegalStatusTransition(SettlementError):
    """Raised when an action is requested from a state that doesn't allow it."""


def _now() -> datetime:
    return datetime.now(UTC)


_SUBMITTABLE = {SettlementStatus.DRAFT}
_AUDITABLE = {SettlementStatus.SUBMITTED, SettlementStatus.RESUBMITTED}
_RESUBMITTABLE = {SettlementStatus.REJECTED}
_PUBLISHABLE = {SettlementStatus.APPROVED}


async def submit_settlement(
    session: AsyncSession,
    *,
    settlement: Settlement,
) -> Settlement:
    if settlement.status not in _SUBMITTABLE:
        raise IllegalStatusTransition(
            f"DRAFT 상태에서만 제출할 수 있습니다 (현재: {settlement.status.value})."
        )
    settlement.status = SettlementStatus.SUBMITTED
    settlement.submitted_at = _now()
    settlement.audited_at = None
    await session.commit()
    await session.refresh(settlement)
    return settlement


async def approve_settlement(
    session: AsyncSession,
    *,
    settlement: Settlement,
) -> Settlement:
    if settlement.status not in _AUDITABLE:
        raise IllegalStatusTransition(
            f"제출됨/재제출됨 상태의 결산안만 승인할 수 있습니다 (현재: {settlement.status.value})."
        )
    settlement.status = SettlementStatus.APPROVED
    settlement.audited_at = _now()
    await session.commit()
    await session.refresh(settlement)
    return settlement


async def reject_settlement(
    session: AsyncSession,
    *,
    settlement: Settlement,
) -> Settlement:
    if settlement.status not in _AUDITABLE:
        raise IllegalStatusTransition(
            f"제출됨/재제출됨 상태의 결산안만 반려할 수 있습니다 (현재: {settlement.status.value})."
        )
    settlement.status = SettlementStatus.REJECTED
    settlement.audited_at = _now()
    await session.commit()
    await session.refresh(settlement)
    return settlement


async def resubmit_settlement(
    session: AsyncSession,
    *,
    settlement: Settlement,
) -> Settlement:
    if settlement.status not in _RESUBMITTABLE:
        raise IllegalStatusTransition(
            f"반려 상태의 결산안만 재제출할 수 있습니다 (현재: {settlement.status.value})."
        )
    settlement.status = SettlementStatus.RESUBMITTED
    settlement.submitted_at = _now()
    # Clear the prior audit timestamp — the resubmission has not been audited
    # yet. The audit comments themselves are preserved as history.
    settlement.audited_at = None
    await session.commit()
    await session.refresh(settlement)
    return settlement


async def publish_settlement(
    session: AsyncSession,
    *,
    settlement: Settlement,
) -> Settlement:
    if settlement.status not in _PUBLISHABLE:
        raise IllegalStatusTransition(
            f"승인된 결산안만 공개할 수 있습니다 (현재: {settlement.status.value})."
        )
    if settlement.published_at is not None:
        raise IllegalStatusTransition("이미 공개된 결산안입니다.")
    settlement.published_at = _now()
    await session.commit()
    await session.refresh(settlement)
    return settlement
