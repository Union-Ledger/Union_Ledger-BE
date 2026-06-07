"""Invitation endpoint tests.

Covers:
  POST   /organizations/{id}/invitations               — Admin issues invite
  GET    /organizations/{id}/invitations               — Admin lists (codes hidden)
  DELETE /organizations/{id}/invitations/{inv_id}      — Admin revokes
  POST   /organizations/{id}/memberships/me/transfer   — role holder hands off
  POST   /invitations/accept                           — already-signed-up user
"""

from __future__ import annotations

from httpx import AsyncClient

from conftest import auth_headers, create_org_as_admin, signup


async def _create_org(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "총학생회",
    college_name: str = "공과대학",
    department_name: str = "컴퓨터공학부",
) -> dict:
    # Self-signup no longer grants ADMIN and POST /organizations is operator-only,
    # so go through the real 회장-application flow (submit → operator approves).
    return await create_org_as_admin(
        client,
        headers,
        name=name,
        college_name=college_name,
        department_name=department_name,
    )


async def _issue_invite(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    org_id: str,
    invited_email: str,
    invitation_type: str = "treasurer_invite",
    role: str = "treasurer",
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/invitations",
        headers=headers,
        json={
            "invitation_type": invitation_type,
            "invited_email": invited_email,
            "role": role,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Issue ----------------------------------------------------------------


async def test_issue_invitation_requires_admin(client: AsyncClient) -> None:
    # Org A's Admin
    await signup(client, email="admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "admin@konkuk.ac.kr")
    org = await _create_org(client, admin_headers)

    # Non-member tries to issue — 403.
    await signup(client, email="stranger@konkuk.ac.kr")
    stranger_headers = await auth_headers(client, "stranger@konkuk.ac.kr")
    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/invitations",
        headers=stranger_headers,
        json={
            "invitation_type": "treasurer_invite",
            "invited_email": "someone@konkuk.ac.kr",
            "role": "treasurer",
        },
    )
    assert resp.status_code == 403, resp.text


async def test_issue_treasurer_invitation_exposes_code_once(
    client: AsyncClient,
) -> None:
    await signup(client, email="boss@konkuk.ac.kr")
    headers = await auth_headers(client, "boss@konkuk.ac.kr")
    org = await _create_org(client, headers)

    issued = await _issue_invite(
        client,
        headers,
        org_id=org["id"],
        invited_email="treasurer.pick@konkuk.ac.kr",
    )
    assert issued["code"], "issuance response must include the code"
    assert issued["status"] == "pending"
    assert issued["role"] == "treasurer"
    assert issued["invitation_type"] == "treasurer_invite"


async def test_issue_auditor_invitation(client: AsyncClient) -> None:
    await signup(client, email="admin3@konkuk.ac.kr")
    headers = await auth_headers(client, "admin3@konkuk.ac.kr")
    org = await _create_org(client, headers)

    issued = await _issue_invite(
        client,
        headers,
        org_id=org["id"],
        invited_email="auditor@konkuk.ac.kr",
        invitation_type="auditor_invite",
        role="auditor",
    )
    assert issued["role"] == "auditor"


async def test_issue_invitation_type_role_mismatch_is_422(client: AsyncClient) -> None:
    await signup(client, email="validator@konkuk.ac.kr")
    headers = await auth_headers(client, "validator@konkuk.ac.kr")
    org = await _create_org(client, headers)

    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/invitations",
        headers=headers,
        json={
            "invitation_type": "treasurer_invite",
            "invited_email": "mismatch@konkuk.ac.kr",
            "role": "auditor",  # wrong role for treasurer_invite
        },
    )
    assert resp.status_code == 422, resp.text


async def test_issue_invitation_rejects_role_transfer_type(
    client: AsyncClient,
) -> None:
    # ROLE_TRANSFER has its own endpoint; direct use of the generic endpoint is
    # rejected with 400 so teammates don't accidentally bypass the transfer flow.
    await signup(client, email="noxfer@konkuk.ac.kr")
    headers = await auth_headers(client, "noxfer@konkuk.ac.kr")
    org = await _create_org(client, headers)

    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/invitations",
        headers=headers,
        json={
            "invitation_type": "role_transfer",
            "invited_email": "successor@konkuk.ac.kr",
            "role": "admin",
        },
    )
    assert resp.status_code == 400, resp.text


# --- List -----------------------------------------------------------------


async def test_list_invitations_never_leaks_code(client: AsyncClient) -> None:
    await signup(client, email="listadmin@konkuk.ac.kr")
    headers = await auth_headers(client, "listadmin@konkuk.ac.kr")
    org = await _create_org(client, headers)

    issued = await _issue_invite(
        client,
        headers,
        org_id=org["id"],
        invited_email="hidden@konkuk.ac.kr",
    )
    real_code = issued["code"]

    resp = await client.get(
        f"/api/v1/organizations/{org['id']}/invitations",
        headers=headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == issued["id"]
    assert items[0]["code"] is None, "list response MUST NOT expose invitation codes"
    # Paranoid: the real code string must not appear anywhere in the listing payload.
    assert real_code not in resp.text


async def test_list_invitations_rejects_non_admin(client: AsyncClient) -> None:
    await signup(client, email="la_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "la_admin@konkuk.ac.kr")
    org = await _create_org(client, admin_headers)

    await signup(client, email="la_other@konkuk.ac.kr")
    other_headers = await auth_headers(client, "la_other@konkuk.ac.kr")
    resp = await client.get(
        f"/api/v1/organizations/{org['id']}/invitations",
        headers=other_headers,
    )
    assert resp.status_code == 403


# --- Revoke ---------------------------------------------------------------


async def test_revoke_pending_invitation(client: AsyncClient) -> None:
    await signup(client, email="revoker@konkuk.ac.kr")
    headers = await auth_headers(client, "revoker@konkuk.ac.kr")
    org = await _create_org(client, headers)
    issued = await _issue_invite(
        client,
        headers,
        org_id=org["id"],
        invited_email="gone@konkuk.ac.kr",
    )

    resp = await client.delete(
        f"/api/v1/organizations/{org['id']}/invitations/{issued['id']}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"
    assert resp.json()["code"] is None


async def test_revoke_unknown_invitation_is_404(client: AsyncClient) -> None:
    await signup(client, email="ghostrevoker@konkuk.ac.kr")
    headers = await auth_headers(client, "ghostrevoker@konkuk.ac.kr")
    org = await _create_org(client, headers)

    resp = await client.delete(
        f"/api/v1/organizations/{org['id']}/invitations/"
        "00000000-0000-0000-0000-000000000000",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_revoked_invitation_cannot_be_accepted(client: AsyncClient) -> None:
    await signup(client, email="revoker2@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "revoker2@konkuk.ac.kr")
    org = await _create_org(client, admin_headers)
    issued = await _issue_invite(
        client,
        admin_headers,
        org_id=org["id"],
        invited_email="invitee@konkuk.ac.kr",
    )
    code = issued["code"]

    revoke = await client.delete(
        f"/api/v1/organizations/{org['id']}/invitations/{issued['id']}",
        headers=admin_headers,
    )
    assert revoke.status_code == 200

    await signup(client, email="invitee@konkuk.ac.kr")
    invitee_headers = await auth_headers(client, "invitee@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/invitations/accept",
        headers=invitee_headers,
        json={"code": code},
    )
    assert resp.status_code == 400, resp.text


# --- Accept ---------------------------------------------------------------


async def test_accept_invitation_grants_membership(client: AsyncClient) -> None:
    await signup(client, email="accept_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "accept_admin@konkuk.ac.kr")
    org = await _create_org(client, admin_headers, name="수락 조직")
    issued = await _issue_invite(
        client,
        admin_headers,
        org_id=org["id"],
        invited_email="new.treasurer@konkuk.ac.kr",
    )

    await signup(client, email="new.treasurer@konkuk.ac.kr", name="재정담당자")
    invitee_headers = await auth_headers(client, "new.treasurer@konkuk.ac.kr")

    resp = await client.post(
        "/api/v1/invitations/accept",
        headers=invitee_headers,
        json={"code": issued["code"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    assert resp.json()["code"] is None

    # /me should now include the treasurer role.
    me = await client.get("/api/v1/auth/me", headers=invitee_headers)
    assert me.status_code == 200
    assert "treasurer" in me.json()["roles"]


async def test_accept_invitation_email_mismatch_is_403(client: AsyncClient) -> None:
    await signup(client, email="em_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "em_admin@konkuk.ac.kr")
    org = await _create_org(client, admin_headers)
    issued = await _issue_invite(
        client,
        admin_headers,
        org_id=org["id"],
        invited_email="intended@konkuk.ac.kr",
    )

    # A different user tries to redeem it.
    await signup(client, email="imposter@konkuk.ac.kr")
    imposter_headers = await auth_headers(client, "imposter@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/invitations/accept",
        headers=imposter_headers,
        json={"code": issued["code"]},
    )
    assert resp.status_code == 403, resp.text


async def test_accept_invitation_unknown_code_is_404(client: AsyncClient) -> None:
    await signup(client, email="unknowncode@konkuk.ac.kr")
    headers = await auth_headers(client, "unknowncode@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/invitations/accept",
        headers=headers,
        json={"code": "this-code-does-not-exist"},
    )
    assert resp.status_code == 404, resp.text


async def test_accept_invitation_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/invitations/accept",
        json={"code": "anything"},
    )
    assert resp.status_code == 401, resp.text


# --- Role transfer --------------------------------------------------------


async def test_role_transfer_hands_off_role(client: AsyncClient) -> None:
    # Seed: admin creates org, then we give user T the treasurer role via invite.
    await signup(client, email="t_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "t_admin@konkuk.ac.kr")
    org = await _create_org(client, admin_headers)
    treasurer_invite = await _issue_invite(
        client,
        admin_headers,
        org_id=org["id"],
        invited_email="tx_outgoing@konkuk.ac.kr",
    )

    await signup(client, email="tx_outgoing@konkuk.ac.kr")
    outgoing_headers = await auth_headers(client, "tx_outgoing@konkuk.ac.kr")
    accepted = await client.post(
        "/api/v1/invitations/accept",
        headers=outgoing_headers,
        json={"code": treasurer_invite["code"]},
    )
    assert accepted.status_code == 200

    # Outgoing treasurer triggers a transfer to the successor.
    xfer = await client.post(
        f"/api/v1/organizations/{org['id']}/memberships/me/transfer",
        headers=outgoing_headers,
        json={"successor_email": "tx_incoming@konkuk.ac.kr"},
    )
    assert xfer.status_code == 201, xfer.text
    assert xfer.json()["invitation_type"] == "role_transfer"
    assert xfer.json()["role"] == "treasurer"
    xfer_code = xfer.json()["code"]
    assert xfer_code

    # Successor signs up and accepts — outgoing should lose the role.
    await signup(client, email="tx_incoming@konkuk.ac.kr")
    incoming_headers = await auth_headers(client, "tx_incoming@konkuk.ac.kr")
    accept_resp = await client.post(
        "/api/v1/invitations/accept",
        headers=incoming_headers,
        json={"code": xfer_code},
    )
    assert accept_resp.status_code == 200, accept_resp.text

    # Re-login both sides so the JWT reflects the latest memberships.
    incoming_me = await client.get(
        "/api/v1/auth/me",
        headers=await auth_headers(client, "tx_incoming@konkuk.ac.kr"),
    )
    outgoing_me = await client.get(
        "/api/v1/auth/me",
        headers=await auth_headers(client, "tx_outgoing@konkuk.ac.kr"),
    )
    assert "treasurer" in incoming_me.json()["roles"]
    assert "treasurer" not in outgoing_me.json()["roles"]


async def test_role_transfer_requires_role_holder(client: AsyncClient) -> None:
    # A user who's ADMIN of their *own* signup-org still has no membership in
    # rt_admin's separate org → nothing to transfer there → 403.
    await signup(client, email="rt_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "rt_admin@konkuk.ac.kr")
    org = await _create_org(client, admin_headers)

    await signup(client, email="rt_student@konkuk.ac.kr")
    student_headers = await auth_headers(client, "rt_student@konkuk.ac.kr")
    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/memberships/me/transfer",
        headers=student_headers,
        json={"successor_email": "ignored@konkuk.ac.kr"},
    )
    assert resp.status_code == 403, resp.text
