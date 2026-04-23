"""Auth flow tests — covers the teammate's send-code → verify → signup → login path."""

from __future__ import annotations

from httpx import AsyncClient

from conftest import DEFAULT_PASSWORD, auth_headers, bearer, login_access_token, signup


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
    # Auto-created org membership grants STUDENT role by default.
    assert "student" in body["roles"]


async def test_signup_duplicate_email_conflicts(client: AsyncClient) -> None:
    await signup(client, email="dup@konkuk.ac.kr")

    send = await client.post(
        "/api/v1/auth/send-verification-code",
        json={"email": "dup@konkuk.ac.kr"},
    )
    code = send.json()["debug_code"]
    await client.post(
        "/api/v1/auth/verify-email",
        json={"email": "dup@konkuk.ac.kr", "code": code},
    )
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


async def test_me_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401, resp.text


async def test_me_rejects_bogus_token(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer this-is-not-a-real-jwt"},
    )
    assert resp.status_code == 401, resp.text


async def test_me_reflects_memberships(client: AsyncClient) -> None:
    await signup(client, email="member@konkuk.ac.kr", name="멤버")
    headers = await auth_headers(client, "member@konkuk.ac.kr")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    # Signup auto-creates an org with the user as STUDENT member.
    assert body["roles"] == ["student"]
