"""Evidence persistence helpers (upload lifecycle beyond the API layer)."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import AuditComment, Evidence, ReconciliationResult


class EvidenceError(Exception):
    """Base for evidence-layer errors (maps to HTTP 4xx)."""


class EvidenceNotFound(EvidenceError):
    pass


async def get_evidence_or_raise(session: AsyncSession, evidence_id: uuid.UUID) -> Evidence:
    evidence = await session.get(Evidence, evidence_id)
    if evidence is None:
        raise EvidenceNotFound("증빙 자료를 찾을 수 없습니다.")
    return evidence


async def delete_evidence(session: AsyncSession, *, evidence: Evidence) -> None:
    """Hard-delete one evidence row, linked comments/reconciliation rows, and file."""
    await session.execute(
        delete(ReconciliationResult).where(
            ReconciliationResult.evidence_id == evidence.id
        )
    )
    await session.execute(
        delete(AuditComment).where(AuditComment.evidence_id == evidence.id)
    )

    file_path = Path(evidence.source_file_path)
    await session.delete(evidence)
    await session.commit()
    file_path.unlink(missing_ok=True)
