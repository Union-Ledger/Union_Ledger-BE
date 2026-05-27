"""Settlement endpoint tests (spec ④).

Covers POST/GET/LIST/PATCH for settlements. A freshly signed-up user is ADMIN
of their auto-created "signup org"; tests that need a fully isolated admin
context still call POST /organizations to spin up a *new* org so fixtures
don't contaminate each other.
"""

from __future__ import annotations

from httpx import AsyncClient

from conftest import auth_headers, create_org_as_admin, signup


async def _create_settlement(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    org_id: str,
    title: str = "2026-1학기 정산",
    academic_year: int = 2026,
    semester: str = "1",
    template_id: str | None = None,
) -> dict:
    body: dict = {
        "title": title,
        "academic_year": academic_year,
        "semester": semester,
    }
    if template_id is not None:
        body["template_id"] = template_id
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json=body,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Create ---------------------------------------------------------------


async def test_create_settlement_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/organizations/00000000-0000-0000-0000-000000000000/settlements",
        json={"title": "x", "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 401, resp.text


async def test_create_settlement_as_admin(client: AsyncClient) -> None:
    await signup(client, email="s_admin@konkuk.ac.kr")
    headers = await auth_headers(client, "s_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)

    settlement = await _create_settlement(client, headers, org_id=org["id"])
    assert settlement["status"] == "draft"
    assert settlement["organization_id"] == org["id"]
    assert settlement["title"] == "2026-1학기 정산"
    assert settlement["template_id"] is None


async def test_create_settlement_rejects_non_member(client: AsyncClient) -> None:
    await signup(client, email="s_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "s_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)

    await signup(client, email="s_outsider@konkuk.ac.kr")
    outsider_headers = await auth_headers(client, "s_outsider@konkuk.ac.kr")
    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=outsider_headers,
        json={"title": "x", "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 403, resp.text


async def test_create_settlement_rejects_invalid_semester(client: AsyncClient) -> None:
    await signup(client, email="s_sem@konkuk.ac.kr")
    headers = await auth_headers(client, "s_sem@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=headers,
        json={"title": "x", "academic_year": 2026, "semester": "spring"},
    )
    assert resp.status_code == 422, resp.text


async def test_create_settlement_rejects_unknown_template(client: AsyncClient) -> None:
    await signup(client, email="s_tmpl@konkuk.ac.kr")
    headers = await auth_headers(client, "s_tmpl@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    resp = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=headers,
        json={
            "title": "x",
            "academic_year": 2026,
            "semester": "1",
            "template_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 404, resp.text


# --- List -----------------------------------------------------------------


async def test_list_settlements_member_only(client: AsyncClient) -> None:
    await signup(client, email="s_la@konkuk.ac.kr")
    headers_a = await auth_headers(client, "s_la@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers_a)
    await _create_settlement(client, headers_a, org_id=org["id"], title="A1")
    await _create_settlement(client, headers_a, org_id=org["id"], title="A2")

    list_resp = await client.get(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=headers_a,
    )
    assert list_resp.status_code == 200
    titles = {s["title"] for s in list_resp.json()}
    assert {"A1", "A2"} == titles

    # Outsider can't list.
    await signup(client, email="s_la_outsider@konkuk.ac.kr")
    headers_b = await auth_headers(client, "s_la_outsider@konkuk.ac.kr")
    forbid = await client.get(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=headers_b,
    )
    assert forbid.status_code == 403


async def test_list_settlements_status_filter(client: AsyncClient) -> None:
    await signup(client, email="s_filter@konkuk.ac.kr")
    headers = await auth_headers(client, "s_filter@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    await _create_settlement(client, headers, org_id=org["id"], title="x")

    # All draft items match the filter.
    drafts = await client.get(
        f"/api/v1/organizations/{org['id']}/settlements?status=draft",
        headers=headers,
    )
    assert drafts.status_code == 200
    assert len(drafts.json()) == 1

    # No approved items yet.
    approved = await client.get(
        f"/api/v1/organizations/{org['id']}/settlements?status=approved",
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json() == []


# --- Detail ---------------------------------------------------------------


async def test_get_settlement_member_only(client: AsyncClient) -> None:
    await signup(client, email="s_d@konkuk.ac.kr")
    headers = await auth_headers(client, "s_d@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s = await _create_settlement(client, headers, org_id=org["id"])

    detail = await client.get(f"/api/v1/settlements/{s['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["id"] == s["id"]

    await signup(client, email="s_d_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "s_d_outsider@konkuk.ac.kr")
    forbid = await client.get(f"/api/v1/settlements/{s['id']}", headers=out_headers)
    assert forbid.status_code == 403


async def test_get_settlement_unknown_id_404(client: AsyncClient) -> None:
    await signup(client, email="s_d_404@konkuk.ac.kr")
    headers = await auth_headers(client, "s_d_404@konkuk.ac.kr")
    resp = await client.get(
        "/api/v1/settlements/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


# --- Patch ----------------------------------------------------------------


async def test_patch_settlement_admin_can_edit_draft(client: AsyncClient) -> None:
    await signup(client, email="s_p@konkuk.ac.kr")
    headers = await auth_headers(client, "s_p@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s = await _create_settlement(client, headers, org_id=org["id"])

    resp = await client.patch(
        f"/api/v1/settlements/{s['id']}",
        headers=headers,
        json={"title": "수정된 제목", "academic_year": 2027},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "수정된 제목"
    assert body["academic_year"] == 2027
    assert body["semester"] == "1"  # untouched


async def test_patch_settlement_empty_body_returns_current_state(
    client: AsyncClient,
) -> None:
    await signup(client, email="s_p_empty@konkuk.ac.kr")
    headers = await auth_headers(client, "s_p_empty@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s = await _create_settlement(client, headers, org_id=org["id"])

    resp = await client.patch(f"/api/v1/settlements/{s['id']}", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == s["title"]


async def test_patch_settlement_rejects_non_treasurer(client: AsyncClient) -> None:
    """The third user has no membership at all in the target org, so 403 —
    being ADMIN of their *own* signup-org doesn't grant cross-org perms."""
    await signup(client, email="s_p_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "s_p_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    s = await _create_settlement(client, owner_headers, org_id=org["id"])

    await signup(client, email="s_p_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "s_p_outsider@konkuk.ac.kr")
    resp = await client.patch(
        f"/api/v1/settlements/{s['id']}",
        headers=out_headers,
        json={"title": "hacked"},
    )
    assert resp.status_code == 403, resp.text


async def test_patch_settlement_unknown_template_404(client: AsyncClient) -> None:
    await signup(client, email="s_p_t404@konkuk.ac.kr")
    headers = await auth_headers(client, "s_p_t404@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s = await _create_settlement(client, headers, org_id=org["id"])

    resp = await client.patch(
        f"/api/v1/settlements/{s['id']}",
        headers=headers,
        json={"template_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404, resp.text


async def test_patch_settlement_invalid_semester_422(client: AsyncClient) -> None:
    await signup(client, email="s_p_sem@konkuk.ac.kr")
    headers = await auth_headers(client, "s_p_sem@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s = await _create_settlement(client, headers, org_id=org["id"])
    resp = await client.patch(
        f"/api/v1/settlements/{s['id']}",
        headers=headers,
        json={"semester": "spring"},
    )
    assert resp.status_code == 422, resp.text
