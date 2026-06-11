from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from pwdlib import PasswordHash

from union_ledger.core.config import get_settings

password_hasher = PasswordHash.recommended()


def verify_password(plain_password: str, password_hash: str) -> bool:
    return password_hasher.verify(plain_password, password_hash)


def hash_password(plain_password: str) -> str:
    return password_hasher.hash(plain_password)


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    to_encode = data.copy()
    expire_delta = expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    expire = datetime.now(UTC) + expire_delta
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    to_encode = data.copy()
    expire_delta = expires_delta or timedelta(days=settings.jwt_refresh_token_expire_days)
    expire = datetime.now(UTC) + expire_delta
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("토큰 검증에 실패했습니다.") from exc
    # 리프레시 토큰으로 일반 API에 접근하는 것을 차단한다(type 없는 기존 토큰은 허용).
    if payload.get("type") == "refresh":
        raise ValueError("리프레시 토큰으로는 접근할 수 없습니다.")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("토큰 검증에 실패했습니다.") from exc
    if payload.get("type") != "refresh":
        raise ValueError("유효한 리프레시 토큰이 아닙니다.")
    return payload
