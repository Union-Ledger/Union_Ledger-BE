"""Audit-flow endpoints (spec ⑧).

State-transition routes are flat (`/settlements/{id}/<action>`) because the
org id is resolved off the loaded settlement, not from the URL.

Role gates (per transition):
  submit, resubmit  → Treasurer / Admin (the team preparing the settlement)
  approve, reject   → Auditor only      (independent reviewer)
  publish           → Admin only        (final release to public mode)
  comment / list    → any org member    (auditor mostly, but treasurer can
                                          reply; we keep it open to the org)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import (
    get_current_user,
    require_membership_for_org,
)
from union_ledger.core.config import get_settings
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Settlement
from union_ledger.models.enums import RoleType
from union_ledger.schemas.audit import (
    AuditCommentCreateRequest,
    AuditCommentResponse,
    AuditDecisionRequest,
)
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.schemas.settlement import SettlementResponse
from union_ledger.services.artifact import generate_settlement_artifacts
from union_ledger.services.audit_comment import (
    EvidenceNotInSettlement,
    create_comment,
    list_comments,
)
from union_ledger.services.audit_workflow import (
    AuditorAccessDenied,
    require_auditor_for_settlement,
)
from union_ledger.services.notification import (
    notify_audit_approved,
    notify_audit_rejected,
    notify_settlement_published,
    notify_settlement_resubmitted,
    notify_settlement_submitted,
)
from union_ledger.services.settlement import (
    SettlementNotFound,
    get_settlement_or_raise,
)
from union_ledger.services.settlement_lifecycle import (
    IllegalStatusTransition,
    approve_settlement,
    publish_settlement,
    reject_settlement,
    resubmit_settlement,
    submit_settlement,
)

router = APIRouter(tags=["audit"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def _load_settlement_or_404(
    session: AsyncSession,
    settlement_id: uuid.UUID,
) -> Settlement:
    try:
        return await get_settlement_or_raise(session, settlement_id)
    except SettlementNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


def _bad_transition(exc: IllegalStatusTransition) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(exc),
    )


async def _maybe_attach_decision_comment(
    session: AsyncSession,
    *,
    settlement: Settlement,
    author_membership_id: uuid.UUID | None,
    comment: str | None,
) -> None:
    """Approve/reject/resubmit may carry an optional comment in the body. We
    attach it as an AuditComment so the audit trail is single-table."""
    if not comment:
        return
    # Re-fetch the membership row only if we have an id — keeps things simple.
    from union_ledger.models.entities import OrganizationMembership

    author = (
        await session.get(OrganizationMembership, author_membership_id)
        if author_membership_id is not None
        else None
    )
    await create_comment(
        session,
        settlement_id=settlement.id,
        author_membership=author,
        comment=comment,
    )


# --- Treasurer / Admin transitions ---------------------------------------


@router.post(
    "/settlements/{settlement_id}/submit",
    response_model=SettlementResponse,
    summary="결산안 제출 (Treasurer/Admin)",
)
async def submit(
    settlement_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )
    try:
        settlement = await submit_settlement(session, settlement=settlement)
    except IllegalStatusTransition as exc:
        raise _bad_transition(exc) from exc
    await generate_settlement_artifacts(
        session,
        settings=get_settings(),
        settlement=settlement,
    )
    await notify_settlement_submitted(
        session, settlement=settlement, actor_user_id=current_user.id
    )
    return SettlementResponse.model_validate(settlement)


@router.post(
    "/settlements/{settlement_id}/resubmit",
    response_model=SettlementResponse,
    summary="결산안 재제출 (Treasurer/Admin)",
)
async def resubmit(
    settlement_id: uuid.UUID,
    payload: AuditDecisionRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    membership = await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.TREASURER, RoleType.PRESIDENT},
    )
    try:
        settlement = await resubmit_settlement(session, settlement=settlement)
    except IllegalStatusTransition as exc:
        raise _bad_transition(exc) from exc
    await generate_settlement_artifacts(
        session,
        settings=get_settings(),
        settlement=settlement,
    )
    await _maybe_attach_decision_comment(
        session,
        settlement=settlement,
        author_membership_id=membership.id,
        comment=payload.comment,
    )
    await notify_settlement_resubmitted(
        session, settlement=settlement, actor_user_id=current_user.id
    )
    return SettlementResponse.model_validate(settlement)


# --- Auditor transitions --------------------------------------------------


@router.post(
    "/settlements/{settlement_id}/audit/approve",
    response_model=SettlementResponse,
    summary="결산안 승인 (Auditor)",
)
async def approve(
    settlement_id: uuid.UUID,
    payload: AuditDecisionRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    try:
        membership = await require_auditor_for_settlement(
            session, user_id=current_user.id, settlement=settlement
        )
    except AuditorAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    try:
        settlement = await approve_settlement(session, settlement=settlement)
    except IllegalStatusTransition as exc:
        raise _bad_transition(exc) from exc
    await _maybe_attach_decision_comment(
        session,
        settlement=settlement,
        author_membership_id=membership.id,
        comment=payload.comment,
    )
    await notify_audit_approved(
        session, settlement=settlement, actor_user_id=current_user.id
    )
    return SettlementResponse.model_validate(settlement)


@router.post(
    "/settlements/{settlement_id}/audit/reject",
    response_model=SettlementResponse,
    summary="결산안 반려 (Auditor)",
)
async def reject(
    settlement_id: uuid.UUID,
    payload: AuditDecisionRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    try:
        membership = await require_auditor_for_settlement(
            session, user_id=current_user.id, settlement=settlement
        )
    except AuditorAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    try:
        settlement = await reject_settlement(session, settlement=settlement)
    except IllegalStatusTransition as exc:
        raise _bad_transition(exc) from exc
    await _maybe_attach_decision_comment(
        session,
        settlement=settlement,
        author_membership_id=membership.id,
        comment=payload.comment,
    )
    await notify_audit_rejected(
        session, settlement=settlement, actor_user_id=current_user.id
    )
    return SettlementResponse.model_validate(settlement)


# --- Admin transition -----------------------------------------------------


@router.post(
    "/settlements/{settlement_id}/publish",
    response_model=SettlementResponse,
    summary="결산안 공개 (Admin)",
)
async def publish(
    settlement_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> SettlementResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
        allowed_roles={RoleType.PRESIDENT},
    )
    try:
        settlement = await publish_settlement(session, settlement=settlement)
    except IllegalStatusTransition as exc:
        raise _bad_transition(exc) from exc
    await notify_settlement_published(
        session, settlement=settlement, actor_user_id=current_user.id
    )
    return SettlementResponse.model_validate(settlement)


# --- Comments -------------------------------------------------------------


@router.post(
    "/settlements/{settlement_id}/comments",
    response_model=AuditCommentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="감사 코멘트 추가 (조직 멤버)",
)
async def add_comment(
    settlement_id: uuid.UUID,
    payload: AuditCommentCreateRequest,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> AuditCommentResponse:
    settlement = await _load_settlement_or_404(session, settlement_id)
    membership = await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    try:
        comment = await create_comment(
            session,
            settlement_id=settlement.id,
            author_membership=membership,
            comment=payload.comment,
            evidence_id=payload.evidence_id,
        )
    except EvidenceNotInSettlement as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return AuditCommentResponse.model_validate(comment)


@router.get(
    "/settlements/{settlement_id}/comments",
    response_model=list[AuditCommentResponse],
    summary="감사 코멘트 목록 (조직 멤버)",
)
async def list_settlement_comments(
    settlement_id: uuid.UUID,
    current_user: Annotated[AuthUser, Depends(get_current_user)],
    session: DbSession,
) -> list[AuditCommentResponse]:
    settlement = await _load_settlement_or_404(session, settlement_id)
    await require_membership_for_org(
        session,
        user_id=current_user.id,
        organization_id=settlement.organization_id,
    )
    items = await list_comments(session, settlement_id=settlement.id)
    return [AuditCommentResponse.model_validate(item) for item in items]
