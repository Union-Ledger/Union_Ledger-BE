from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from union_ledger.core.config import get_settings


@dataclass(slots=True)
class _CodeEntry:
    code: str
    expires_at: datetime


@dataclass(slots=True)
class _VerifiedEntry:
    expires_at: datetime


class EmailVerificationStore:
    def __init__(
        self,
        *,
        code_ttl: timedelta = timedelta(minutes=5),
        verified_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._code_ttl = code_ttl
        self._verified_ttl = verified_ttl
        self._codes: dict[str, _CodeEntry] = {}
        self._verified: dict[str, _VerifiedEntry] = {}
        self._lock = Lock()

    def issue_code(self, email: str) -> tuple[str, int]:
        now = datetime.now(UTC)
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = now + self._code_ttl
        normalized = email.strip().lower()

        with self._lock:
            self._cleanup(now)
            self._codes[normalized] = _CodeEntry(code=code, expires_at=expires_at)
            self._verified.pop(normalized, None)

        return code, int(self._code_ttl.total_seconds())

    def verify_code(self, email: str, code: str) -> bool:
        now = datetime.now(UTC)
        normalized = email.strip().lower()

        with self._lock:
            self._cleanup(now)
            entry = self._codes.get(normalized)
            if entry is None or entry.code != code:
                return False
            self._codes.pop(normalized, None)
            self._verified[normalized] = _VerifiedEntry(expires_at=now + self._verified_ttl)
            return True

    def revoke_code(self, email: str) -> None:
        normalized = email.strip().lower()
        with self._lock:
            self._codes.pop(normalized, None)

    def is_verified(self, email: str) -> bool:
        now = datetime.now(UTC)
        normalized = email.strip().lower()

        with self._lock:
            self._cleanup(now)
            return normalized in self._verified

    def consume_verified(self, email: str) -> bool:
        now = datetime.now(UTC)
        normalized = email.strip().lower()

        with self._lock:
            self._cleanup(now)
            if normalized not in self._verified:
                return False
            self._verified.pop(normalized, None)
            return True

    def _cleanup(self, now: datetime) -> None:
        expired_codes = [email for email, entry in self._codes.items() if entry.expires_at <= now]
        expired_verified = [
            email for email, entry in self._verified.items() if entry.expires_at <= now
        ]
        for email in expired_codes:
            self._codes.pop(email, None)
        for email in expired_verified:
            self._verified.pop(email, None)


email_verification_store = EmailVerificationStore()


def reconfigure_email_verification_store() -> None:
    settings = get_settings()
    email_verification_store._code_ttl = timedelta(
        minutes=settings.email_verification_code_expire_minutes
    )
    email_verification_store._verified_ttl = timedelta(
        minutes=settings.email_verification_verified_expire_minutes
    )
