"""Shared test fixtures.

Goals:
- Run each test against a fresh in-memory aiosqlite database so the suite
  stays fast and isolated.
- Make the teammate's auth flow (send-code → verify-email → signup) ergonomic
  inside tests: `debug_code` is exposed only when `settings.debug=True`, and
  the real SMTP call is replaced with a no-op so tests don't need a mail
  server.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# --- Settings overrides ---------------------------------------------------
# `get_settings()` is @lru_cache'd, and many modules call it at import time.
# We set env vars *before* importing the app, then clear the cache on every
# test so each run observes them. `JWT_SECRET_KEY` is set to a stable dummy
# value; the default "change-me-in-env" works too but being explicit makes
# debugging failures easier.
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SMTP_ENABLED", "false")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-do-not-use-in-prod-32chars-min")
os.environ.setdefault("OPERATOR_EMAILS", "operator@konkuk.ac.kr,ops@example.com")

from union_ledger.core.config import get_settings  # noqa: E402
from union_ledger.db.session import get_db_session  # noqa: E402
from union_ledger.main import app  # noqa: E402
from union_ledger.models.base import Base  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real SMTP send with a no-op for every test.

    The auth router imports `send_verification_email` by name into its own
    module namespace, so patching the function at `services.email_sender`
    wouldn't take effect — we patch the attribute on the endpoint module
    directly.
    """
    from union_ledger.api.v1.endpoints import auth as auth_ep

    def _noop(recipient: str, code: str, expires_in_seconds: int) -> None:
        return None

    monkeypatch.setattr(auth_ep, "send_verification_email", _noop)


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db_session, None)


# --- Helpers --------------------------------------------------------------

DEFAULT_PASSWORD = "TestPass123!"
DEFAULT_COLLEGE = "공과대학"
DEFAULT_DEPARTMENT = "컴퓨터공학부"
# Must match the OPERATOR_EMAILS env var set above. An operator is just a normal
# user whose email is on the allowlist; they review 회장 applications.
OPERATOR_EMAIL = "operator@konkuk.ac.kr"
# A non-konkuk operator email — exercises the domain-bypass for operators.
OPERATOR_EMAIL_EXTERNAL = "ops@example.com"


async def signup(
    client: AsyncClient,
    *,
    email: str,
    password: str = DEFAULT_PASSWORD,
    name: str = "홍길동",
    college_name: str = DEFAULT_COLLEGE,
    department_name: str = DEFAULT_DEPARTMENT,
    invitation_code: str | None = None,
) -> str:
    """Full signup path (send-code → verify-email → signup). Returns access_token.

    Relies on `DEBUG=true` so `debug_code` is included in the send-code
    response. `_stub_smtp` keeps the email send from hitting the network.
    """
    send = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": email},
    )
    assert send.status_code == 200, send.text
    code = send.json()["debug_code"]
    assert code, "debug build should expose debug_code"

    verify = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": email, "code": code},
    )
    assert verify.status_code == 200, verify.text

    payload = {
        "name": name,
        "email": email,
        "password": password,
        "password_confirm": password,
        "college_name": college_name,
        "department_name": department_name,
    }
    if invitation_code is not None:
        payload["invitation_code"] = invitation_code

    signup_resp = await client.post("/api/v1/auth/signup", json=payload)
    assert signup_resp.status_code == 200, signup_resp.text
    return signup_resp.json()["access_token"]


async def login_access_token(
    client: AsyncClient,
    *,
    email: str,
    password: str = DEFAULT_PASSWORD,
) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def auth_headers(
    client: AsyncClient,
    email: str,
    password: str = DEFAULT_PASSWORD,
) -> dict[str, str]:
    token = await login_access_token(client, email=email, password=password)
    return bearer(token)


async def add_auditor_to_org(
    client: AsyncClient,
    *,
    org_id: str,
    admin_headers: dict[str, str],
    auditor_email: str,
) -> dict[str, str]:
    """Issue an auditor invite, sign up the auditor user, and accept it.

    Returns the auditor's bearer auth headers. The auditor will hold:
      - ADMIN in their auto-created signup-org (irrelevant here)
      - AUDITOR in `org_id`
    """
    issue = await client.post(
        f"/api/v1/organizations/{org_id}/invitations",
        headers=admin_headers,
        json={
            "invitation_type": "auditor_invite",
            "invited_email": auditor_email,
            "role": "auditor",
        },
    )
    assert issue.status_code == 201, issue.text
    code = issue.json()["code"]

    await signup(client, email=auditor_email, name="감사위원")
    auditor_headers = await auth_headers(client, auditor_email)

    accept = await client.post(
        "/api/v1/invitations/accept",
        headers=auditor_headers,
        json={"code": code},
    )
    assert accept.status_code == 200, accept.text

    # Re-issue token so memberships reflect the new role.
    return await auth_headers(client, auditor_email)


async def add_treasurer_to_org(
    client: AsyncClient,
    *,
    org_id: str,
    admin_headers: dict[str, str],
    treasurer_email: str,
) -> dict[str, str]:
    """Mirror of `add_auditor_to_org` for the TREASURER role."""
    issue = await client.post(
        f"/api/v1/organizations/{org_id}/invitations",
        headers=admin_headers,
        json={
            "invitation_type": "treasurer_invite",
            "invited_email": treasurer_email,
            "role": "treasurer",
        },
    )
    assert issue.status_code == 201, issue.text
    code = issue.json()["code"]

    await signup(client, email=treasurer_email, name="재정담당")
    treasurer_headers = await auth_headers(client, treasurer_email)
    accept = await client.post(
        "/api/v1/invitations/accept",
        headers=treasurer_headers,
        json={"code": code},
    )
    assert accept.status_code == 200, accept.text
    return await auth_headers(client, treasurer_email)


async def operator_headers(client: AsyncClient) -> dict[str, str]:
    """Auth headers for the platform operator (email on the OPERATOR_EMAILS
    allowlist). Signs the operator up once, then reuses it within the test."""
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": OPERATOR_EMAIL, "password": DEFAULT_PASSWORD},
    )
    if login.status_code != 200:
        await signup(client, email=OPERATOR_EMAIL, name="운영자")
    return await auth_headers(client, OPERATOR_EMAIL)


async def create_org_as_admin(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "총학생회",
    college_name: str = DEFAULT_COLLEGE,
    department_name: str = DEFAULT_DEPARTMENT,
) -> dict:
    """Make the caller ADMIN of a new org via the real 회장-application flow:
    the caller submits an application (with a proof doc), then an operator
    approves it. Returns a dict with the created org's id + identifying fields.

    Self-signup no longer grants ADMIN, so this is how tests obtain an
    admin-owned org. The returned shape stays {id, name, college_name,
    department_name} so existing callers keep working.
    """
    app_resp = await client.post(
        "/api/v1/admin-applications",
        headers=headers,
        data={
            "organization_name": name,
            "college_name": college_name,
            "department_name": department_name,
        },
        files={"documents": ("proof.pdf", b"%PDF-1.4 fake proof", "application/pdf")},
    )
    assert app_resp.status_code == 201, app_resp.text
    application_id = app_resp.json()["id"]

    op_headers = await operator_headers(client)
    approve = await client.post(
        f"/api/v1/admin-applications/{application_id}/approve",
        headers=op_headers,
    )
    assert approve.status_code == 200, approve.text
    return {
        "id": approve.json()["organization_id"],
        "name": name,
        "college_name": college_name,
        "department_name": department_name,
    }
