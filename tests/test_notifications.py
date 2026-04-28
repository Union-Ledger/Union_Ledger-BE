"""Notification tests (spec §5-2).

Covers:
  - Lifecycle fanout: submit → auditor; approve/reject → treasurer/admin;
    resubmit → auditor; publish → org students
  - Inbox listing with unread filter and counts
  - Mark-as-read endpoint with cross-user isolation
"""

from __future__ import annotations

from httpx import AsyncClient

from conftest import (
    add_auditor_to_org,
    add_treasurer_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)


async def _create_settlement(
    client: AsyncClient, headers: dict[str, str], *, org_id: str
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json={"title": "결산", "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Submit fans out to auditors -----------------------------------------


async def test_submit_notifies_auditor(client: AsyncClient) -> None:
    await signup(client, email="n_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_auditor@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    submit = await client.post(
        f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers
    )
    assert submit.status_code == 200

    inbox = await client.get("/api/v1/notifications", headers=auditor_headers)
    assert inbox.status_code == 200, inbox.text
    body = inbox.json()
    assert body["total"] == 1
    assert body["unread"] == 1
    assert body["items"][0]["notification_type"] == "settlement_submitted"
    assert body["items"][0]["read_at"] is None


# --- Approve / reject fan out to treasurers/admins ----------------------


async def test_approve_notifies_treasurer_and_admin(client: AsyncClient) -> None:
    await signup(client, email="n_a_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_a_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_a_auditor@konkuk.ac.kr",
    )
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        treasurer_email="n_a_treasurer@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)

    approve = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    assert approve.status_code == 200, approve.text

    # Treasurer received approval notification
    treasurer_inbox = await client.get(
        "/api/v1/notifications", headers=treasurer_headers
    )
    types = [n["notification_type"] for n in treasurer_inbox.json()["items"]]
    assert "audit_approved" in types

    # Admin (who actually triggered submit but is *not* the actor of approve)
    # also receives the approval notification — they hold ADMIN role.
    admin_inbox = await client.get("/api/v1/notifications", headers=admin_headers)
    admin_types = [n["notification_type"] for n in admin_inbox.json()["items"]]
    assert "audit_approved" in admin_types


async def test_reject_notifies_treasurer(client: AsyncClient) -> None:
    await signup(client, email="n_r_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_r_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_r_auditor@konkuk.ac.kr",
    )
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        treasurer_email="n_r_treasurer@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    reject = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/reject",
        headers=auditor_headers,
        json={"comment": "다시 작성해주세요"},
    )
    assert reject.status_code == 200, reject.text

    inbox = await client.get("/api/v1/notifications", headers=treasurer_headers)
    items = inbox.json()["items"]
    assert any(n["notification_type"] == "audit_rejected" for n in items)


# --- Resubmit fans out back to auditors ---------------------------------


async def test_resubmit_notifies_auditor(client: AsyncClient) -> None:
    await signup(client, email="n_re_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_re_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_re_auditor@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    await client.post(
        f"/api/v1/settlements/{s['id']}/audit/reject",
        headers=auditor_headers,
        json={},
    )
    re = await client.post(
        f"/api/v1/settlements/{s['id']}/resubmit",
        headers=admin_headers,
        json={"comment": "수정 완료"},
    )
    assert re.status_code == 200, re.text

    inbox = await client.get("/api/v1/notifications", headers=auditor_headers)
    types = [n["notification_type"] for n in inbox.json()["items"]]
    assert "settlement_resubmitted" in types


# --- Publish fans out to all org Students -------------------------------


async def test_publish_notifies_org_members(client: AsyncClient) -> None:
    """Publish fans out to every member of the org regardless of role
    (treasurer + auditor here)."""
    await signup(client, email="n_p_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_p_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_p_auditor@konkuk.ac.kr",
    )
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        treasurer_email="n_p_treasurer@konkuk.ac.kr",
    )

    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    pub = await client.post(
        f"/api/v1/settlements/{s['id']}/publish", headers=admin_headers
    )
    assert pub.status_code == 200, pub.text

    # Auditor receives publish notification (different from earlier ones).
    auditor_types = [
        n["notification_type"]
        for n in (await client.get(
            "/api/v1/notifications", headers=auditor_headers
        )).json()["items"]
    ]
    assert "settlement_published" in auditor_types

    # Treasurer also receives it.
    treasurer_types = [
        n["notification_type"]
        for n in (await client.get(
            "/api/v1/notifications", headers=treasurer_headers
        )).json()["items"]
    ]
    assert "settlement_published" in treasurer_types


# --- Inbox UX -----------------------------------------------------------


async def test_only_unread_filter(client: AsyncClient) -> None:
    await signup(client, email="n_u_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_u_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_u_auditor@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)

    inbox = await client.get("/api/v1/notifications", headers=auditor_headers)
    nid = inbox.json()["items"][0]["id"]

    read_resp = await client.post(
        f"/api/v1/notifications/{nid}/read", headers=auditor_headers
    )
    assert read_resp.status_code == 200, read_resp.text
    assert read_resp.json()["read_at"] is not None

    only_unread = await client.get(
        "/api/v1/notifications?only_unread=true", headers=auditor_headers
    )
    assert only_unread.json()["items"] == []
    assert only_unread.json()["unread"] == 0
    assert only_unread.json()["total"] == 1


async def test_mark_read_other_users_notification_404(client: AsyncClient) -> None:
    await signup(client, email="n_x_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_x_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="n_x_auditor@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    nid = (await client.get("/api/v1/notifications", headers=auditor_headers)).json()[
        "items"
    ][0]["id"]

    # An outsider user must not be able to mark it read.
    await signup(client, email="n_x_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "n_x_outsider@konkuk.ac.kr")
    resp = await client.post(
        f"/api/v1/notifications/{nid}/read", headers=out_headers
    )
    assert resp.status_code == 404, resp.text


# --- Actor exclusion ----------------------------------------------------


async def test_actor_does_not_receive_own_notification(client: AsyncClient) -> None:
    """If the auditor approves their own settlement (rare but possible if
    they hold both Auditor + Treasurer/Admin roles), they shouldn't receive
    the approve notification.

    Setup: admin creates settlement, *also* invites auditor role for self via
    the auditor invite flow → admin holds ADMIN + AUDITOR. Approve as admin
    → no AUDIT_APPROVED notification arrives for admin.
    """
    await signup(client, email="n_self@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "n_self@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)

    issue = await client.post(
        f"/api/v1/organizations/{org['id']}/invitations",
        headers=admin_headers,
        json={
            "invitation_type": "auditor_invite",
            "invited_email": "n_self@konkuk.ac.kr",
            "role": "auditor",
        },
    )
    code = issue.json()["code"]
    accept = await client.post(
        "/api/v1/invitations/accept",
        headers=admin_headers,
        json={"code": code},
    )
    assert accept.status_code == 200
    admin_headers = await auth_headers(client, "n_self@konkuk.ac.kr")

    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    approve = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=admin_headers,
        json={},
    )
    assert approve.status_code == 200, approve.text

    inbox = await client.get("/api/v1/notifications", headers=admin_headers)
    types = [n["notification_type"] for n in inbox.json()["items"]]
    # Admin self-submitted → no SETTLEMENT_SUBMITTED for self,
    # Admin self-approved → no AUDIT_APPROVED for self.
    assert "audit_approved" not in types
    assert "settlement_submitted" not in types
