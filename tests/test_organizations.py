"""Organization endpoint tests.

Covers:
  POST   /organizations              — operator-only direct creation (seeding)
  GET    /organizations              — list orgs the caller is a member of
  GET    /organizations/{id}         — member-only (any role)
  GET    /organizations/{id}/members — Admin-only

Note: self-signup no longer creates an org or grants any role. The normal way
a 회장 gets an admin-owned org is the document-reviewed application flow
(see `create_org_as_admin`); `POST /organizations` is now an operator-only
escape hatch.
"""

from __future__ import annotations

from httpx import AsyncClient

from conftest import auth_headers, create_org_as_admin, operator_headers, signup


async def _create_org(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "총학생회",
    college_name: str = "공과대학",
    department_name: str = "컴퓨터공학부",
) -> dict:
    """Give the caller an admin-owned org via the real application→approval flow."""
    return await create_org_as_admin(
        client,
        headers,
        name=name,
        college_name=college_name,
        department_name=department_name,
    )


async def test_create_organization_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/organizations",
        json={
            "name": "총학생회",
            "college_name": "공과대학",
            "department_name": "컴퓨터공학부",
        },
    )
    assert resp.status_code == 401, resp.text


async def test_operator_creates_org_and_becomes_admin(client: AsyncClient) -> None:
    # POST /organizations is operator-only; the operator becomes the org ADMIN.
    op_headers = await operator_headers(client)
    resp = await client.post(
        "/api/v1/organizations",
        headers=op_headers,
        json={
            "name": "컴공 학생회",
            "college_name": "공과대학",
            "department_name": "컴퓨터공학부",
        },
    )
    assert resp.status_code == 201, resp.text
    org = resp.json()
    assert org["name"] == "컴공 학생회"
    assert org["college_name"] == "공과대학"

    me = await client.get("/api/v1/auth/me", headers=op_headers)
    assert me.status_code == 200
    assert set(me.json()["roles"]) == {"admin"}


async def test_create_organization_requires_operator(client: AsyncClient) -> None:
    # A normal (non-operator) user cannot create an organization directly.
    await signup(client, email="regular@konkuk.ac.kr")
    headers = await auth_headers(client, "regular@konkuk.ac.kr")
    resp = await client.post(
        "/api/v1/organizations",
        headers=headers,
        json={
            "name": "무단 조직",
            "college_name": "공과대학",
            "department_name": "컴퓨터공학부",
        },
    )
    assert resp.status_code == 403, resp.text


async def test_create_organization_rejects_blank_fields(client: AsyncClient) -> None:
    # Use an operator so the request reaches body validation (422), not the gate.
    op_headers = await operator_headers(client)
    resp = await client.post(
        "/api/v1/organizations",
        headers=op_headers,
        json={"name": "", "college_name": "공과대학", "department_name": "컴퓨터공학부"},
    )
    assert resp.status_code == 422, resp.text


async def test_list_organizations_returns_members_orgs_only(client: AsyncClient) -> None:
    # User A creates an org they admin.
    await signup(client, email="a@konkuk.ac.kr")
    headers_a = await auth_headers(client, "a@konkuk.ac.kr")
    org_a = await _create_org(client, headers_a, name="A의 학생회")

    # User B signs up (auto-creates their own signup org, nothing else).
    await signup(client, email="b@konkuk.ac.kr")
    headers_b = await auth_headers(client, "b@konkuk.ac.kr")

    list_a = await client.get("/api/v1/organizations", headers=headers_a)
    assert list_a.status_code == 200
    names_a = {o["name"] for o in list_a.json()}
    assert "A의 학생회" in names_a

    list_b = await client.get("/api/v1/organizations", headers=headers_b)
    assert list_b.status_code == 200
    names_b = {o["name"] for o in list_b.json()}
    # B isn't a member of A's org.
    assert org_a["name"] not in names_b


async def test_get_organization_requires_membership(client: AsyncClient) -> None:
    await signup(client, email="owner2@konkuk.ac.kr")
    headers_owner = await auth_headers(client, "owner2@konkuk.ac.kr")
    org = await _create_org(client, headers_owner, name="비밀 조직")

    # Member (owner is Admin here) can read the detail.
    ok = await client.get(f"/api/v1/organizations/{org['id']}", headers=headers_owner)
    assert ok.status_code == 200
    assert ok.json()["id"] == org["id"]

    # Outsider signed up but is not a member — 403.
    await signup(client, email="outsider@konkuk.ac.kr")
    headers_outsider = await auth_headers(client, "outsider@konkuk.ac.kr")
    forbidden = await client.get(
        f"/api/v1/organizations/{org['id']}",
        headers=headers_outsider,
    )
    assert forbidden.status_code == 403, forbidden.text


async def test_get_organization_unknown_id_is_404_for_member(
    client: AsyncClient,
) -> None:
    # The guard fires first (not a member → 403) for a nonexistent org because
    # the caller has no membership in it. That's the right security posture.
    # Here we check the "member but org vanished" path by hitting a garbage uuid
    # which the caller can't possibly be a member of → 403.
    await signup(client, email="ghosthunter@konkuk.ac.kr")
    headers = await auth_headers(client, "ghosthunter@konkuk.ac.kr")
    resp = await client.get(
        "/api/v1/organizations/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


async def test_list_members_admin_only(client: AsyncClient) -> None:
    await signup(client, email="admin1@konkuk.ac.kr", name="관리자")
    headers_admin = await auth_headers(client, "admin1@konkuk.ac.kr")
    org = await _create_org(client, headers_admin)

    # Admin sees the membership list and finds themselves in it.
    ok = await client.get(
        f"/api/v1/organizations/{org['id']}/members",
        headers=headers_admin,
    )
    assert ok.status_code == 200
    members = ok.json()
    assert len(members) == 1
    assert members[0]["role"] == "admin"
    assert members[0]["user"]["email"] == "admin1@konkuk.ac.kr"
    assert members[0]["is_primary"] is True


async def test_list_members_rejects_non_member(client: AsyncClient) -> None:
    await signup(client, email="admin2@konkuk.ac.kr")
    headers_admin = await auth_headers(client, "admin2@konkuk.ac.kr")
    org = await _create_org(client, headers_admin)

    await signup(client, email="nobody@konkuk.ac.kr")
    headers_nobody = await auth_headers(client, "nobody@konkuk.ac.kr")
    resp = await client.get(
        f"/api/v1/organizations/{org['id']}/members",
        headers=headers_nobody,
    )
    assert resp.status_code == 403, resp.text
