"""Auth flow tests — covers the teammate's send-code → verify → signup → login path."""

from __future__ import annotations

from httpx import AsyncClient

from conftest import (
    DEFAULT_PASSWORD,
    OPERATOR_EMAIL_EXTERNAL,
    auth_headers,
    bearer,
    create_org_as_admin,
    login_access_token,
    signup,
)


async def test_send_verification_code_returns_debug_code(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "new.user@konkuk.ac.kr"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["debug_code"], "DEBUG=true in tests"
    assert body["expires_in_seconds"] > 0


async def test_send_verification_code_rejects_non_konkuk_email(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "outsider@gmail.com"},
    )
    assert resp.status_code == 400, resp.text


async def test_verify_email_with_correct_code(client: AsyncClient) -> None:
    send = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "verify.me@konkuk.ac.kr"},
    )
    code = send.json()["debug_code"]

    verify = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": "verify.me@konkuk.ac.kr", "code": code},
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["verified"] is True


async def test_verify_email_wrong_code_fails(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "wrong.code@konkuk.ac.kr"},
    )
    resp = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": "wrong.code@konkuk.ac.kr", "code": "000000"},
    )
    assert resp.status_code == 400, resp.text


async def test_signup_requires_verified_email(client: AsyncClient) -> None:
    # No send-verification-code → no verified state cached.
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "name": "무검증",
            "email": "unverified@konkuk.ac.kr",
            "password": DEFAULT_PASSWORD,
            "password_confirm": DEFAULT_PASSWORD,
            "college_name": "공과대학",
            "department_name": "컴퓨터공학부",
        },
    )
    assert resp.status_code == 400, resp.text


async def test_signup_rejects_mismatched_passwords(client: AsyncClient) -> None:
    send = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "mismatch@konkuk.ac.kr"},
    )
    code = send.json()["debug_code"]
    await client.post(
        "/api/v1/auth/verify-email",
        json={"email": "mismatch@konkuk.ac.kr", "code": code},
    )
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "name": "불일치",
            "email": "mismatch@konkuk.ac.kr",
            "password": "TestPass123!",
            "password_confirm": "OtherPass!!",
            "college_name": "공과대학",
            "department_name": "컴퓨터공학부",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_signup_returns_access_token(client: AsyncClient) -> None:
    token = await signup(client, email="happy.path@konkuk.ac.kr", name="행복")
    assert token

    me = await client.get("/api/v1/auth/me", headers=bearer(token))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == "happy.path@konkuk.ac.kr"
    assert body["name"] == "행복"
    # No-invitation signup is a plain student account — no org, no role.
    assert body["roles"] == []


async def test_signup_duplicate_email_conflicts(client: AsyncClient) -> None:
    await signup(client, email="dup@konkuk.ac.kr")

    send = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "dup@konkuk.ac.kr"},
    )
    assert send.status_code == 409, send.text

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "name": "중복",
            "email": "dup@konkuk.ac.kr",
            "password": DEFAULT_PASSWORD,
            "password_confirm": DEFAULT_PASSWORD,
            "college_name": "공과대학",
            "department_name": "컴퓨터공학부",
        },
    )
    assert resp.status_code == 409, resp.text


async def test_verify_email_rejects_existing_account(client: AsyncClient) -> None:
    await signup(client, email="already@konkuk.ac.kr")

    send = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "fresh@konkuk.ac.kr"},
    )
    code = send.json()["debug_code"]

    # Swap to an existing email after obtaining a code for a different address.
    verify = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": "already@konkuk.ac.kr", "code": code},
    )
    assert verify.status_code == 409, verify.text


async def test_login_wrong_password(client: AsyncClient) -> None:
    await signup(client, email="badpw@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "badpw@konkuk.ac.kr", "password": "WrongPassword!"},
    )
    assert resp.status_code == 401, resp.text


async def test_login_unknown_email(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@konkuk.ac.kr", "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 401, resp.text


async def test_login_ok(client: AsyncClient) -> None:
    await signup(client, email="loginok@konkuk.ac.kr")
    token = await login_access_token(client, email="loginok@konkuk.ac.kr")
    assert token


async def test_login_normalizes_email_case(client: AsyncClient) -> None:
    # Signup stores emails lowercased; login must match regardless of the
    # casing/whitespace the user types (e.g. mobile auto-capitalization).
    await signup(client, email="casing@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "  Casing@Konkuk.AC.kr ", "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


async def test_me_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401, resp.text


async def test_me_rejects_bogus_token(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer this-is-not-a-real-jwt"},
    )
    assert resp.status_code == 401, resp.text


# --- Password reset (비밀번호 찾기) -------------------------------------


async def test_forgot_password_returns_debug_code_for_existing_user(
    client: AsyncClient,
) -> None:
    await signup(client, email="forgot.me@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "forgot.me@konkuk.ac.kr"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["debug_code"], "DEBUG=true exposes the reset code"


async def test_forgot_password_unknown_email_no_enumeration(
    client: AsyncClient,
) -> None:
    # Unknown account: still 200, but no code is issued (debug_code is null).
    resp = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "nobody.here@konkuk.ac.kr"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["debug_code"] is None


async def test_forgot_password_rejects_non_konkuk_email(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "outsider@gmail.com"},
    )
    assert resp.status_code == 400, resp.text


async def test_reset_password_full_flow_and_login(client: AsyncClient) -> None:
    await signup(client, email="reset.flow@konkuk.ac.kr")

    forgot = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "reset.flow@konkuk.ac.kr"},
    )
    code = forgot.json()["debug_code"]

    new_password = "BrandNewPass1!"
    reset = await client.post(
        "/api/v1/auth/password/reset",
        json={
            "email": "reset.flow@konkuk.ac.kr",
            "code": code,
            "new_password": new_password,
            "new_password_confirm": new_password,
        },
    )
    assert reset.status_code == 200, reset.text

    # Old password no longer works.
    old = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset.flow@konkuk.ac.kr", "password": DEFAULT_PASSWORD},
    )
    assert old.status_code == 401, old.text

    # New password works.
    new = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset.flow@konkuk.ac.kr", "password": new_password},
    )
    assert new.status_code == 200, new.text


async def test_reset_password_wrong_code_fails(client: AsyncClient) -> None:
    await signup(client, email="reset.badcode@konkuk.ac.kr")
    await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "reset.badcode@konkuk.ac.kr"},
    )
    resp = await client.post(
        "/api/v1/auth/password/reset",
        json={
            "email": "reset.badcode@konkuk.ac.kr",
            "code": "000000",
            "new_password": "Whatever123!",
            "new_password_confirm": "Whatever123!",
        },
    )
    assert resp.status_code == 400, resp.text


async def test_reset_password_mismatch_is_422(client: AsyncClient) -> None:
    await signup(client, email="reset.mismatch@konkuk.ac.kr")
    forgot = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "reset.mismatch@konkuk.ac.kr"},
    )
    code = forgot.json()["debug_code"]
    resp = await client.post(
        "/api/v1/auth/password/reset",
        json={
            "email": "reset.mismatch@konkuk.ac.kr",
            "code": code,
            "new_password": "Whatever123!",
            "new_password_confirm": "Different123!",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_reset_password_rejects_same_as_current(client: AsyncClient) -> None:
    await signup(client, email="reset.same@konkuk.ac.kr")
    forgot = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "reset.same@konkuk.ac.kr"},
    )
    code = forgot.json()["debug_code"]
    resp = await client.post(
        "/api/v1/auth/password/reset",
        json={
            "email": "reset.same@konkuk.ac.kr",
            "code": code,
            "new_password": DEFAULT_PASSWORD,
            "new_password_confirm": DEFAULT_PASSWORD,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "기존 비밀번호" in resp.json()["detail"]


async def test_me_reflects_memberships(client: AsyncClient) -> None:
    await signup(client, email="member@konkuk.ac.kr", name="멤버")
    headers = await auth_headers(client, "member@konkuk.ac.kr")
    # A plain signup holds no role.
    before = await client.get("/api/v1/auth/me", headers=headers)
    assert before.status_code == 200
    assert before.json()["roles"] == []

    # After an operator approves their 회장 application they hold ADMIN, and
    # /me reflects it (the role gate re-queries memberships, not the token).
    await create_org_as_admin(client, headers)
    after = await client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 200
    assert after.json()["roles"] == ["president"]


# --- Operators (allowlist) may use a non-konkuk email --------------------


async def test_operator_with_external_email_can_signup(client: AsyncClient) -> None:
    """An OPERATOR_EMAILS account may use any domain — the konkuk-only check is
    bypassed for operators — and /auth/me flags them as an operator."""
    token = await signup(client, email=OPERATOR_EMAIL_EXTERNAL, name="운영자")
    assert token
    me = await client.get("/api/v1/auth/me", headers=bearer(token))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == OPERATOR_EMAIL_EXTERNAL
    assert body["is_operator"] is True


async def test_non_operator_non_konkuk_email_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "random.person@gmail.com"},
    )
    assert resp.status_code == 400, resp.text


async def test_me_is_operator_false_for_regular_user(client: AsyncClient) -> None:
    await signup(client, email="plain.user@konkuk.ac.kr")
    headers = await auth_headers(client, "plain.user@konkuk.ac.kr")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["is_operator"] is False


async def test_me_includes_primary_organization(client: AsyncClient) -> None:
    await signup(client, email="me_org@konkuk.ac.kr")
    headers = await auth_headers(client, "me_org@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["organization_id"] == org["id"]
    assert len(body["organizations"]) == 1
    assert body["organizations"][0]["id"] == org["id"]
    assert body["organizations"][0]["role"] == "president"
    assert body["organizations"][0]["is_primary"] is True


async def test_me_organization_id_null_without_membership(
    client: AsyncClient,
) -> None:
    await signup(client, email="me_noorg@konkuk.ac.kr")
    headers = await auth_headers(client, "me_noorg@konkuk.ac.kr")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["organization_id"] is None
    assert body["organizations"] == []
