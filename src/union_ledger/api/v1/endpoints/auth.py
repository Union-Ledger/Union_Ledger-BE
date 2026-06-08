from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.api.deps.auth import get_current_user
from union_ledger.core.config import get_settings
from union_ledger.core.security import create_access_token, hash_password, verify_password
from union_ledger.db.session import get_db_session
from union_ledger.models.entities import Invitation, Organization, OrganizationMembership, User
from union_ledger.models.enums import InvitationStatus, RoleType
from union_ledger.schemas.auth_request import (
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    SendVerificationCodeRequest,
    SignUpRequest,
    VerifyEmailCodeRequest,
)
from union_ledger.schemas.auth_response import (
    AuthUser,
    ResetPasswordResponse,
    SendVerificationCodeResponse,
    TokenResponse,
    VerifyEmailCodeResponse,
)
from union_ledger.services.email_verification_store import (
    consume_verified,
    issue_code,
    revoke_code,
    verify_code,
)
from union_ledger.services.email_sender import EmailDeliveryError, send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])


def _validate_university_email(email: str) -> str:
    normalized = email.strip().lower()
    # Platform operators (maintainers) are allowlisted by email and may use any
    # domain — they aren't necessarily students/staff with a konkuk address.
    operator_emails = {item.lower() for item in get_settings().operator_emails}
    if normalized in operator_emails:
        return normalized
    if not normalized.endswith("@konkuk.ac.kr"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="학교 이메일(@konkuk.ac.kr)만 인증할 수 있습니다.",
        )
    return normalized


@router.post(
    "/send-verification-code",
    response_model=SendVerificationCodeResponse,
    summary="Issue temporary email verification code",
)
async def send_verification_code(
    payload: SendVerificationCodeRequest,
    session: AsyncSession = Depends(get_db_session),
) -> SendVerificationCodeResponse:
    email = _validate_university_email(payload.email)
    code, expires_in = await issue_code(session, email)

    try:
        await asyncio.to_thread(send_verification_email, email, code, expires_in)
    except EmailDeliveryError as exc:
        await revoke_code(session, email)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    settings = get_settings()
    return SendVerificationCodeResponse(
        message="인증 코드가 발급되었습니다.",
        expires_in_seconds=expires_in,
        debug_code=code if settings.debug else None,
    )


@router.post(
    "/verify-email",
    response_model=VerifyEmailCodeResponse,
    summary="Verify university email with temporary code",
)
async def verify_email(
    payload: VerifyEmailCodeRequest,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyEmailCodeResponse:
    email = _validate_university_email(payload.email)
    verified = await verify_code(session, email, payload.code.strip())
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증 코드가 올바르지 않거나 만료되었습니다.",
        )
    return VerifyEmailCodeResponse(message="이메일 인증이 완료되었습니다.", verified=True)


@router.post(
    "/password/forgot",
    response_model=SendVerificationCodeResponse,
    summary="비밀번호 재설정 코드 발급 (비밀번호 찾기)",
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_db_session),
) -> SendVerificationCodeResponse:
    email = _validate_university_email(payload.email)
    user = await session.scalar(select(User).where(User.email == email))

    reconfigure_email_verification_store()
    settings = get_settings()

    # Avoid account enumeration: respond identically whether or not the email
    # has an account. We only actually issue + send a code for real accounts.
    if user is None or not user.is_active:
        return SendVerificationCodeResponse(
            message="가입된 계정인 경우 재설정 코드를 발송했습니다.",
            expires_in_seconds=settings.email_verification_code_expire_minutes * 60,
            debug_code=None,
        )

    code, expires_in = email_verification_store.issue_code(email)
    try:
        await asyncio.to_thread(send_verification_email, email, code, expires_in)
    except EmailDeliveryError as exc:
        email_verification_store.revoke_code(email)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return SendVerificationCodeResponse(
        message="가입된 계정인 경우 재설정 코드를 발송했습니다.",
        expires_in_seconds=expires_in,
        debug_code=code if settings.debug else None,
    )


@router.post(
    "/password/reset",
    response_model=ResetPasswordResponse,
    summary="비밀번호 재설정 (코드 검증 후 새 비밀번호 저장)",
)
async def reset_password(
    payload: ResetPasswordRequest,
    session: AsyncSession = Depends(get_db_session),
) -> ResetPasswordResponse:
    email = _validate_university_email(payload.email)

    reconfigure_email_verification_store()
    verified = email_verification_store.verify_code(email, payload.code.strip())
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증 코드가 올바르지 않거나 만료되었습니다.",
        )

    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="가입된 계정을 찾을 수 없습니다.",
        )

    user.password_hash = hash_password(payload.new_password)
    await session.commit()

    # The code was single-use; clear any lingering verified state so it can't
    # be reused for signup.
    email_verification_store.consume_verified(email)

    return ResetPasswordResponse(message="비밀번호가 재설정되었습니다.")


@router.post("/login", response_model=TokenResponse, summary="Issue JWT access token")
async def login_for_access_token(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    # Signup stores the email normalized (strip + lower); normalize here too so
    # login isn't case/whitespace-sensitive (e.g. mobile auto-capitalization).
    email = payload.email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="이메일 또는 비밀번호가 올바르지 않습니다.",
    )

    if user is None or not user.is_active or not user.password_hash:
        raise credentials_exception

    if not verify_password(payload.password, user.password_hash):
        raise credentials_exception

    roles_result = await session.scalars(
        select(OrganizationMembership.role).where(OrganizationMembership.user_id == user.id)
    )
    role_values = sorted({role.value for role in roles_result.all()})

    token = create_access_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "roles": role_values,
        }
    )
    return TokenResponse(access_token=token)


@router.post("/signup", response_model=TokenResponse, summary="Create account and issue JWT")
async def sign_up(
    payload: SignUpRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    email = _validate_university_email(payload.email)
    existing_user = await session.scalar(select(User).where(User.email == email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 가입된 이메일입니다.",
        )

    if not await consume_verified(session, email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이메일 인증이 완료되지 않았습니다.",
        )

    invitation: Invitation | None = None
    organization: Organization | None = None
    membership_role = RoleType.STUDENT

    if payload.invitation_code:
        invitation = await session.scalar(
            select(Invitation).where(Invitation.code == payload.invitation_code)
        )
        if invitation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="유효하지 않은 초대 코드입니다.",
            )
        if invitation.status != InvitationStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="사용할 수 없는 초대 코드입니다.",
            )
        if invitation.invited_email.lower() != email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="초대 코드와 이메일이 일치하지 않습니다.",
            )
        organization = await session.get(Organization, invitation.organization_id)
        if organization is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="초대된 조직을 찾을 수 없습니다.",
            )
        membership_role = invitation.role

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        name=payload.name,
        college_name=payload.college_name,
        department_name=payload.department_name,
        university_email_verified=True,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    # No invitation = a plain student account. Email verification proves the
    # person belongs to the school, which is enough to browse published
    # settlements; students get NO organization membership (and thus no role).
    # Becoming a 회장 (org ADMIN) now requires an operator-approved application
    # (see /admin-applications) — self-signup no longer grants any role, which
    # closes the old "first signup silently becomes ADMIN of a new org" gap.
    if organization is not None:
        membership = OrganizationMembership(
            organization_id=organization.id,
            user_id=user.id,
            role=membership_role,
            is_primary=True,
        )
        session.add(membership)

    if invitation is not None:
        invitation.status = InvitationStatus.ACCEPTED

    await session.commit()
    await session.refresh(user)

    roles_result = await session.scalars(
        select(OrganizationMembership.role).where(OrganizationMembership.user_id == user.id)
    )
    role_values = sorted({role.value for role in roles_result.all()})

    token = create_access_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "roles": role_values,
        }
    )
    return TokenResponse(access_token=token)


@router.get("/me", response_model=AuthUser, summary="Get current authenticated user")
async def read_current_user(current_user: AuthUser = Depends(get_current_user)) -> AuthUser:
    return current_user
