"""Student viewer + dashboard endpoints."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from conftest import (
    add_auditor_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)


async def _approve_and_publish(
    client: AsyncClient,
    *,
    settlement_id: str,
    admin_headers: dict[str, str],
    auditor_headers: dict[str, str],
) -> None:
    assert (
        await client.post(
            f"/api/v1/settlements/{settlement_id}/submit",
            headers=admin_headers,
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/settlements/{settlement_id}/audit/approve",
            headers=auditor_headers,
            json={},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/settlements/{settlement_id}/publish",
            headers=admin_headers,
        )
    ).status_code == 200


async def test_student_dashboard_no_settlement_yet(client: AsyncClient) -> None:
    email = f"stu_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"
    await signup(client, email=email)
    headers = await auth_headers(client, email)

    resp = await client.get("/api/v1/dashboard/student", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["organization"]["college_name"] == "공과대학"
    cp = body["current_period"]
    assert cp["status_label"] == "미제출"
    assert cp["is_published"] is False
    assert len(cp["steps"]) == 3
    assert "title" not in cp
    assert "college_period_overview" in body
    assert isinstance(body["college_period_overview"], list)


async def test_period_cards_audit_in_progress_and_published(
    client: AsyncClient,
) -> None:
    college = "공과대학"
    dept_a = "컴퓨터공학부"
    dept_b = "전자공학부"

    admin_a = f"stu_a_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"
    admin_b = f"stu_b_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"
    viewer = f"stu_v_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"

    await signup(client, email=admin_a, department_name=dept_a)
    headers_a = await auth_headers(client, admin_a)
    org_a = await create_org_as_admin(
        client,
        headers_a,
        name="컴공 학생회",
        college_name=college,
        department_name=dept_a,
    )

    await signup(client, email=admin_b, department_name=dept_b)
    headers_b = await auth_headers(client, admin_b)
    org_b = await create_org_as_admin(
        client,
        headers_b,
        name="전자 학생회",
        college_name=college,
        department_name=dept_b,
    )

    auditor_a = await add_auditor_to_org(
        client,
        org_id=org_a["id"],
        admin_headers=headers_a,
        auditor_email=f"stu_aud_a_{uuid.uuid4().hex[:6]}@konkuk.ac.kr",
    )

    s_a = await client.post(
        f"/api/v1/organizations/{org_a['id']}/settlements",
        headers=headers_a,
        json={"title": "A", "academic_year": 2026, "semester": "1"},
    )
    assert s_a.status_code == 201
    settlement_a_id = s_a.json()["id"]
    assert (
        await client.post(
            f"/api/v1/settlements/{settlement_a_id}/submit",
            headers=headers_a,
        )
    ).status_code == 200

    s_b = await client.post(
        f"/api/v1/organizations/{org_b['id']}/settlements",
        headers=headers_b,
        json={"title": "B", "academic_year": 2026, "semester": "1"},
    )
    assert s_b.status_code == 201
    settlement_b_id = s_b.json()["id"]
    auditor_b = await add_auditor_to_org(
        client,
        org_id=org_b["id"],
        admin_headers=headers_b,
        auditor_email=f"stu_aud_b_{uuid.uuid4().hex[:6]}@konkuk.ac.kr",
    )
    await _approve_and_publish(
        client,
        settlement_id=settlement_b_id,
        admin_headers=headers_b,
        auditor_headers=auditor_b,
    )

    await signup(client, email=viewer, college_name=college, department_name=dept_a)
    viewer_headers = await auth_headers(client, viewer)

    periods = await client.get("/api/v1/public/periods", headers=viewer_headers)
    assert periods.status_code == 200
    assert any(
        p["academic_year"] == 2026 and p["semester"] == "1" for p in periods.json()
    )

    cards = await client.get(
        "/api/v1/public/periods/2026/1/settlements",
        headers=viewer_headers,
    )
    assert cards.status_code == 200, cards.text
    by_name = {c["organization_name"]: c for c in cards.json()}

    assert by_name["컴공 학생회"]["status_label"] == "감사 중"
    assert by_name["컴공 학생회"]["is_published"] is False
    assert by_name["전자 학생회"]["status_label"] == "공개 완료"
    assert by_name["전자 학생회"]["is_published"] is True
    assert "title" not in by_name["전자 학생회"]

    # Primary (viewer signup org) listed first when present in overview.
    overview = cards.json()
    assert overview[0]["is_my_organization"] is True

    dash = await client.get("/api/v1/dashboard/student", headers=viewer_headers)
    assert dash.status_code == 200
    college_overview = dash.json()["college_period_overview"]
    assert len(college_overview) >= 2
    assert college_overview[0]["is_my_organization"] is True
    assert any(c["organization_name"] == "전자 학생회" for c in college_overview)


async def test_view_count_increments_when_published(
    client: AsyncClient,
) -> None:
    email = f"stu_vc_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"
    await signup(client, email=email)
    headers = await auth_headers(client, email)

    org = await create_org_as_admin(client, headers, name="조회수 학생회")
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=headers,
        auditor_email=f"stu_aud_vc_{uuid.uuid4().hex[:6]}@konkuk.ac.kr",
    )

    created = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=headers,
        json={"title": "t", "academic_year": 2026, "semester": "1"},
    )
    assert created.status_code == 201
    sid = created.json()["id"]

    not_pub = await client.post(
        f"/api/v1/public/settlements/{sid}/views",
        headers=headers,
    )
    assert not_pub.status_code == 404

    await _approve_and_publish(
        client,
        settlement_id=sid,
        admin_headers=headers,
        auditor_headers=auditor_headers,
    )

    view1 = await client.post(
        f"/api/v1/public/settlements/{sid}/views",
        headers=headers,
    )
    assert view1.status_code == 200
    assert view1.json()["view_count"] == 1

    view2 = await client.post(
        f"/api/v1/public/settlements/{sid}/views",
        headers=headers,
    )
    assert view2.json()["view_count"] == 2

    dash = await client.get(
        f"/api/v1/dashboard/student?organization_id={org['id']}",
        headers=headers,
    )
    assert dash.status_code == 200
    assert dash.json()["summary"]["total_view_count"] >= 2
