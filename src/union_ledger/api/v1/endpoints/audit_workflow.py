"""Auditor-side workflow endpoints (spec §3 + API spec /audit/*).

Routes:
  GET   /audit/settlements              — auditor worklist (scoped to caller's
                                          AUDITOR memberships, status filter)
  GET   /audit/settlements/{id}         — comparison-view bundle
  PATCH /audit/comments/{id}            — edit comment (author only)

Authorization split from the main audit lifecycle file (`audit.py`):
  - The lifecycle endpoints (submit/approve/reject/...) gate on a *specific*
    role per action.
  - The worklist endpoint gates by "is the caller an auditor anywhere?" and
    then scopes results to those orgs inside the service.
  - The review-bundle endpoint requires the caller to be an auditor in the
    settlement's org (any org member could view via the existing
    `GET /settlements/{id}` route — this endpoint is the auditor-specific
    aggregated payload, so we tighten the gate).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import (
    get_current_user,
    require_membership_for_org,
)
from union_ledger.db.session import get_db_session
from union_ledger.models.enums import MatchStatus, RoleType, SettlementStatus
from union_ledger.schemas.audit import AuditCommentResponse
from union_ledger.schemas.audit_workflow import (
    AuditCommentUpdateRequest,
    AuditReconciliationSummary,
    AuditReviewBundle,
    AuditWorklistItem,
)
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.bank_statement import BankTransactionResponse
from union_ledger.schemas.evidence import EvidenceResponse
from union_ledger.schemas.settlement import SettlementResponse
from union_ledger.services.audit_workflow import (
    CommentNotEditableByUser,
    CommentNotFound,
    get_review_bundle,
    list_auditor_worklist,
    update_comment,
)
from union_ledger.services.reconciliation import build_result_responses
from union_ledger.services.settlement import (
    SettlementNotFound,
    get_settlement_or_raise,
)

router = APIRouter(prefix="/audit", tags=["audit-workflow"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/settlements",
    response_model=list[AuditWorklistItem],
    summary="감사위원 워크리스트 (내가 감사 권한을 가진 조직의 결산안)",
)
async def list_worklist(
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    statuses: Annotated[
        list[SettlementStatus] | None,
        Query(
            alias="status",
            description=(
                "필터할 상태 목록. 비워두면 DRAFT 외 전체. "
                "예: ?status=submitted&status=resubmitted (대기중만)"
            ),
        ),
    ] = None,
) -> list[AuditWorklistItem]:
    if RoleType.AUDITOR not in set(current_user.roles):
        # Cheap precheck — saves a DB roundtrip for non-auditors. The service
        # also gracefully returns [] if the user has no auditor memberships.
        return []

    entries = await list_auditor_worklist(
        session,
        user_id=current_user.id,
        statuses=statuses,
    )
    return [
        AuditWorklistItem(
            settlement_id=e.settlement.id,
            organization_id=e.organization.id,
            organization_name=e.organization.name,
            college_name=e.organization.college_name,
            department_name=e.organization.department_name,
            title=e.settlement.title,
            academic_year=e.settlement.academic_year,
            semester=e.settlement.semester,
            status=e.settlement.status,
            submitted_at=e.settlement.submitted_at,
            audited_at=e.settlement.audited_at,
            evidence_count=e.evidence_count,
            bank_transaction_count=e.bank_transaction_count,
            audit_comment_count=e.audit_comment_count,
            total_evidence_amount=e.total_evidence_amount,
            reconciliation=AuditReconciliationSummary(
                matched=e.reconciliation_counts.get(MatchStatus.MATCHED, 0),
                amount_mismatch=e.reconciliation_counts.get(
                    MatchStatus.AMOUNT_MISMATCH, 0
                ),
                date_mismatch=e.reconciliation_counts.get(
                    MatchStatus.DATE_MISMATCH, 0
                ),
                missing_bank_transaction=e.reconciliation_counts.get(
                    MatchStatus.MISSING_BANK_TRANSACTION, 0
                ),
                missing_evidence=e.reconciliation_counts.get(
                    MatchStatus.MISSING_EVIDENCE, 0
                ),
                manually_resolved=e.reconciliation_counts.get(
                    MatchStatus.MANUALLY_RESOLVED, 0
                ),
            ),
        )
        for e in entries
    ]


@router.get(
    "/settlements/{settlement_id}",
    response_model=AuditReviewBundle,
    summary="감사 비교 검토 화면 데이터 (Auditor 전용)",
)
async def get_review(
    settlement_id: uuid.UUID,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> AuditReviewBundle:
    try:
        settlement = await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.AUDITOR},
    )

    bundle = await get_review_bundle(session, settlement=settlement)
    reconciliation_results = await build_result_responses(
        session, bundle.reconciliation_results
    )
    return AuditReviewBundle(
        settlement=SettlementResponse.model_validate(bundle.settlement),
        evidences=[EvidenceResponse.model_validate(e) for e in bundle.evidences],
        bank_transactions=[
            BankTransactionResponse.model_validate(tx)
            for tx in bundle.bank_transactions
        ],
        reconciliation_results=reconciliation_results,
        comments=[AuditCommentResponse.model_validate(c) for c in bundle.comments],
    )


@router.patch(
    "/comments/{comment_id}",
    response_model=AuditCommentResponse,
    summary="감사 코멘트 수정 (작성자 본인만)",
)
async def patch_comment(
    comment_id: uuid.UUID,
    payload: AuditCommentUpdateRequest,
    session: DbSession,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
) -> AuditCommentResponse:
    try:
        comment = await update_comment(
            session,
            comment_id=comment_id,
            user_id=current_user.id,
            new_text=payload.comment,
        )
    except CommentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except CommentNotEditableByUser as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return AuditCommentResponse.model_validate(comment)
