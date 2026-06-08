"""Email-verification state, persisted in the database.

Replaces the old in-memory dict store so verification codes and the 'verified'
flag survive process restarts and are shared across workers/replicas. State
lives in the ``email_verifications`` table (one row per email); TTLs come from
``Settings`` and are evaluated on read.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.config import get_settings
from union_ledger.models.entities import EmailVerification


def _normalize(email: str) -> str:
    return email.strip().lower()


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; treat stored timestamps as UTC.
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _get_row(session: AsyncSession, email: str) -> EmailVerification | None:
    return await session.scalar(
        select(EmailVerification).where(EmailVerification.email == email)
    )


async def issue_code(session: AsyncSession, email: str) -> tuple[str, int]:
    """Issue a fresh 6-digit code, invalidating any prior code + verified flag.

    Returns (code, ttl_seconds).
    """
    settings = get_settings()
    code_ttl = timedelta(minutes=settings.email_verification_code_expire_minutes)
    now = datetime.now(UTC)
    normalized = _normalize(email)
    code = f"{secrets.randbelow(1_000_000):06d}"

    row = await _get_row(session, normalized)
    if row is None:
        row = EmailVerification(email=normalized)
        session.add(row)
    row.code = code
    row.code_expires_at = now + code_ttl
    row.verified_until = None
    await session.commit()
    return code, int(code_ttl.total_seconds())


async def verify_code(session: AsyncSession, email: str, code: str) -> bool:
    """Validate the code; on success consume it and set the verified flag."""
    settings = get_settings()
    verified_ttl = timedelta(
        minutes=settings.email_verification_verified_expire_minutes
    )
    now = datetime.now(UTC)

    row = await _get_row(session, _normalize(email))
    if row is None or row.code is None or row.code != code:
        return False
    expires_at = _aware(row.code_expires_at)
    if expires_at is None or expires_at <= now:
        return False

    row.code = None
    row.code_expires_at = None
    row.verified_until = now + verified_ttl
    await session.commit()
    return True


async def revoke_code(session: AsyncSession, email: str) -> None:
    """Clear a pending code (e.g. when the verification email failed to send)."""
    row = await _get_row(session, _normalize(email))
    if row is not None and row.code is not None:
        row.code = None
        row.code_expires_at = None
        await session.commit()


async def is_verified(session: AsyncSession, email: str) -> bool:
    now = datetime.now(UTC)
    row = await _get_row(session, _normalize(email))
    verified_until = _aware(row.verified_until) if row else None
    return verified_until is not None and verified_until > now


async def consume_verified(session: AsyncSession, email: str) -> bool:
    """One-time check at signup: True if currently verified, then clears it."""
    now = datetime.now(UTC)
    row = await _get_row(session, _normalize(email))
    if row is None:
        return False
    verified_until = _aware(row.verified_until)
    if verified_until is None or verified_until <= now:
        return False
    row.verified_until = None
    await session.commit()
    return True
