from __future__ import annotations

import uuid

from pydantic import BaseModel

from union_ledger.models.enums import RoleType


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthUser(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    roles: list[RoleType]
    # True when the account is on the OPERATOR_EMAILS allowlist (platform
    # maintainer). Lets the FE show the operator-only review section.
    is_operator: bool = False


class OrganizationSummary(BaseModel):
    id: uuid.UUID
    name: str
    college_name: str | None = None
    department_name: str | None = None
    role: RoleType
    is_primary: bool


class MeResponse(AuthUser):
    """`/auth/me` payload — AuthUser plus the caller's organization context so
    the FE can resolve the active organization directly instead of guessing or
    falling back to localStorage. `organization_id` is the primary membership
    (is_primary, else the first); None for a plain student with no membership."""

    organization_id: uuid.UUID | None = None
    organizations: list[OrganizationSummary] = []


class SendVerificationCodeResponse(BaseModel):
    message: str
    expires_in_seconds: int
    debug_code: str | None = None


class VerifyEmailCodeResponse(BaseModel):
    message: str
    verified: bool


class ResetPasswordResponse(BaseModel):
    message: str