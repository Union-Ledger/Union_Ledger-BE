"""회장(admin) application + operator review tests.

Covers the only sanctioned path to an org-ADMIN role: a school-verified user
submits proof documents, and a platform operator (OPERATOR_EMAILS allowlist)
approves or rejects. Approval creates the org and grants the applicant ADMIN.
"""

from __future__ import annotations

from httpx import AsyncClient

from conftest import auth_headers, operator_headers, signup


def _doc_file() -> dict:
    return {"documents": ("proof.pdf", b"%PDF-1.4 proof document", "application/pdf")}


async def _submit(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "공과대학 학생회",
    college: str = "공과대학",
    dept: str = "컴퓨터공학부",
):
    return await client.post(
        "/api/v1/admin-applications",
        headers=headers,
        data={
            "organization_name": name,
            "college_name": college,
            "department_name": dept,
        },
        files=_doc_file(),
    )


# --- Submit --------------------------------------------------------------


async def test_submit_application_and_list_me(client: AsyncClient) -> None:
    await signup(client, email="applicant@konkuk.ac.kr")
    headers = await auth_headers(client, "applicant@konkuk.ac.kr")

    resp = await _submit(client, headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert len(body["documents"]) == 1
    assert body["documents"][0]["file_name"] == "proof.pdf"
    # The on-disk path must never leak to clients.
    assert "file_path" not in body["documents"][0]

    mine = await client.get("/api/v1/admin-applications/me", headers=headers)
    assert mine.status_code == 200
    assert len(mine.json()) == 1


async def test_submit_requires_a_document(client: AsyncClient) -> None:
    await signup(client, email="nodoc@konkuk.ac.kr")
    headers = await auth_headers(client, "nodoc@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/admin-applications",
        headers=headers,
        data={
            "organization_name": "x",
            "college_name": "y",
            "department_name": "z",
        },
    )
    assert resp.status_code in (400, 422), resp.text


# --- Operator review -----------------------------------------------------


async def test_non_operator_cannot_list_all(client: AsyncClient) -> None:
    await signup(client, email="snoop@konkuk.ac.kr")
    headers = await auth_headers(client, "snoop@konkuk.ac.kr")
    resp = await client.get("/api/v1/admin-applications", headers=headers)
    assert resp.status_code == 403, resp.text


async def test_approve_creates_org_and_grants_admin(client: AsyncClient) -> None:
    await signup(client, email="chair@konkuk.ac.kr")
    headers = await auth_headers(client, "chair@konkuk.ac.kr")
    application_id = (await _submit(client, headers)).json()["id"]

    op = await operator_headers(client)
    pending = await client.get("/api/v1/admin-applications?status=pending", headers=op)
    assert pending.status_code == 200
    assert any(item["id"] == application_id for item in pending.json())

    approve = await client.post(
        f"/api/v1/admin-applications/{application_id}/approve",
        headers=op,
        json={"note": "학생회장 확인됨"},
    )
    assert approve.status_code == 200, approve.text
    org_id = approve.json()["organization_id"]
    assert approve.json()["application"]["status"] == "approved"

    # The applicant is now ADMIN of the created org: an admin-only endpoint works
    # even though their token predates the role (org gates re-query the DB).
    members = await client.get(
        f"/api/v1/organizations/{org_id}/members", headers=headers
    )
    assert members.status_code == 200, members.text
    dash = await client.get("/api/v1/dashboard/treasurer", headers=headers)
    assert dash.json()["organization_count"] == 1


async def test_approve_twice_conflicts(client: AsyncClient) -> None:
    await signup(client, email="chair2@konkuk.ac.kr")
    headers = await auth_headers(client, "chair2@konkuk.ac.kr")
    application_id = (await _submit(client, headers)).json()["id"]
    op = await operator_headers(client)

    first = await client.post(
        f"/api/v1/admin-applications/{application_id}/approve", headers=op
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/api/v1/admin-applications/{application_id}/approve", headers=op
    )
    assert second.status_code == 409, second.text


async def test_reject_with_note(client: AsyncClient) -> None:
    await signup(client, email="chair3@konkuk.ac.kr")
    headers = await auth_headers(client, "chair3@konkuk.ac.kr")
    application_id = (await _submit(client, headers)).json()["id"]
    op = await operator_headers(client)

    resp = await client.post(
        f"/api/v1/admin-applications/{application_id}/reject",
        headers=op,
        json={"note": "서류 불충분"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert resp.json()["review_note"] == "서류 불충분"


async def test_non_operator_cannot_approve(client: AsyncClient) -> None:
    await signup(client, email="chair4@konkuk.ac.kr")
    headers = await auth_headers(client, "chair4@konkuk.ac.kr")
    application_id = (await _submit(client, headers)).json()["id"]
    # The applicant is not an operator and cannot approve their own application.
    resp = await client.post(
        f"/api/v1/admin-applications/{application_id}/approve", headers=headers
    )
    assert resp.status_code == 403, resp.text


# --- Document access -----------------------------------------------------


async def test_owner_views_outsider_blocked(client: AsyncClient) -> None:
    await signup(client, email="owner@konkuk.ac.kr")
    owner = await auth_headers(client, "owner@konkuk.ac.kr")
    application_id = (await _submit(client, owner)).json()["id"]

    detail = await client.get(
        f"/api/v1/admin-applications/{application_id}", headers=owner
    )
    assert detail.status_code == 200, detail.text
    doc = await client.get(
        f"/api/v1/admin-applications/{application_id}/documents/0", headers=owner
    )
    assert doc.status_code == 200, doc.text

    await signup(client, email="outsider@konkuk.ac.kr")
    outsider = await auth_headers(client, "outsider@konkuk.ac.kr")
    assert (
        await client.get(
            f"/api/v1/admin-applications/{application_id}", headers=outsider
        )
    ).status_code == 403
    assert (
        await client.get(
            f"/api/v1/admin-applications/{application_id}/documents/0",
            headers=outsider,
        )
    ).status_code == 403


async def test_operator_can_download_document(client: AsyncClient) -> None:
    await signup(client, email="chair5@konkuk.ac.kr")
    headers = await auth_headers(client, "chair5@konkuk.ac.kr")
    application_id = (await _submit(client, headers)).json()["id"]
    op = await operator_headers(client)
    doc = await client.get(
        f"/api/v1/admin-applications/{application_id}/documents/0", headers=op
    )
    assert doc.status_code == 200, doc.text
