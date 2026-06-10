"""Auditor-side workflow tests (spec §3 + API spec /audit/*).

Covers:
  - Worklist scoping: only orgs where caller is AUDITOR; status filter
  - Review bundle: includes evidences, bank tx, reconciliation rows, comments
  - Comment edit: author-only gate, 404 / 403 paths
"""

from __future__ import annotations

import io
from datetime import date

from httpx import AsyncClient
from openpyxl import Workbook

from conftest import (
    add_auditor_to_org,
    add_treasurer_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)


def _build_xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _create_settlement(
    client: AsyncClient, headers: dict[str, str], *, org_id: str, title: str = "결산"
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json={"title": title, "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Worklist -----------------------------------------------------------


async def test_worklist_returns_only_auditor_orgs(client: AsyncClient) -> None:
    """A college auditor sees every department in their college, not other colleges."""
    await signup(client, email="aw_a@konkuk.ac.kr")
    a_admin = await auth_headers(client, "aw_a@konkuk.ac.kr")
    org_a = await create_org_as_admin(client, a_admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org_a["id"],
        admin_headers=a_admin,
        auditor_email="aw_auditor@konkuk.ac.kr",
    )
    s_a = await _create_settlement(client, a_admin, org_id=org_a["id"], title="A")
    await client.post(f"/api/v1/settlements/{s_a['id']}/submit", headers=a_admin)

    # Same college, different department — should be visible.
    await signup(client, email="aw_b@konkuk.ac.kr")
    b_admin = await auth_headers(client, "aw_b@konkuk.ac.kr")
    org_b = await create_org_as_admin(
        client,
        b_admin,
        name="B팀",
        college_name="공과대학",
        department_name="기계공학부",
    )
    s_b = await _create_settlement(client, b_admin, org_id=org_b["id"], title="B")
    await client.post(f"/api/v1/settlements/{s_b['id']}/submit", headers=b_admin)

    # Different college — must stay hidden.
    await signup(client, email="aw_c@konkuk.ac.kr")
    c_admin = await auth_headers(client, "aw_c@konkuk.ac.kr")
    org_c = await create_org_as_admin(
        client,
        c_admin,
        name="C팀",
        college_name="경영대학",
        department_name="경영학부",
    )
    s_c = await _create_settlement(client, c_admin, org_id=org_c["id"], title="C")
    await client.post(f"/api/v1/settlements/{s_c['id']}/submit", headers=c_admin)

    list_resp = await client.get(
        "/api/v1/audit/settlements", headers=auditor_headers
    )
    assert list_resp.status_code == 200, list_resp.text
    titles = {item["title"] for item in list_resp.json()}
    assert titles == {"A", "B"}


async def test_worklist_status_filter(client: AsyncClient) -> None:
    await signup(client, email="aw_f@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_f@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="aw_f_auditor@konkuk.ac.kr",
    )
    # One submitted, one approved.
    s1 = await _create_settlement(client, admin, org_id=org["id"], title="open")
    s2 = await _create_settlement(client, admin, org_id=org["id"], title="closed")
    await client.post(f"/api/v1/settlements/{s1['id']}/submit", headers=admin)
    await client.post(f"/api/v1/settlements/{s2['id']}/submit", headers=admin)
    await client.post(
        f"/api/v1/settlements/{s2['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )

    pending = await client.get(
        "/api/v1/audit/settlements?status=submitted&status=resubmitted",
        headers=auditor_headers,
    )
    assert pending.status_code == 200
    pending_titles = {row["title"] for row in pending.json()}
    assert pending_titles == {"open"}

    done = await client.get(
        "/api/v1/audit/settlements?status=approved",
        headers=auditor_headers,
    )
    done_titles = {row["title"] for row in done.json()}
    assert done_titles == {"closed"}


async def test_worklist_empty_for_non_auditor(client: AsyncClient) -> None:
    """A user with no auditor membership gets [] (not 403)."""
    await signup(client, email="aw_n@konkuk.ac.kr")
    headers = await auth_headers(client, "aw_n@konkuk.ac.kr")
    resp = await client.get("/api/v1/audit/settlements", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_worklist_aggregates_counts(client: AsyncClient) -> None:
    """Worklist row should expose evidence/tx/comment counts + reconciliation
    summary so the FE can paint the dashboard without N+1 queries."""
    await signup(client, email="aw_c@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_c@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    treasurer = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        treasurer_email="aw_c_treasurer@konkuk.ac.kr",
    )
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="aw_c_auditor@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin, org_id=org["id"])

    # Upload one bank statement → 2 transactions.
    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "T1", -10000],
            [date(2026, 4, 2), "T2", -20000],
        ]
    )
    up = await client.post(
        f"/api/v1/settlements/{s['id']}/bank-statements",
        headers=treasurer,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    assert up.status_code == 201

    # Submit so it shows in the worklist.
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin)

    # Auditor adds a comment.
    cm = await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=auditor_headers,
        json={"comment": "확인 필요"},
    )
    assert cm.status_code == 201

    # Run reconciliation (no evidence yet → 2 MISSING_EVIDENCE rows).
    await client.post(
        f"/api/v1/settlements/{s['id']}/reconciliation:run",
        headers=treasurer,
    )

    list_resp = await client.get(
        "/api/v1/audit/settlements", headers=auditor_headers
    )
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["organization_name"] == org["name"]
    assert row["bank_transaction_count"] == 2
    assert row["audit_comment_count"] == 1
    assert row["evidence_count"] == 0
    assert row["reconciliation"]["missing_evidence"] == 2
    assert row["reconciliation"]["matched"] == 0


# --- Review bundle ------------------------------------------------------


async def test_review_bundle_includes_all_sections(client: AsyncClient) -> None:
    await signup(client, email="aw_b1@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_b1@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    treasurer = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        treasurer_email="aw_b1_t@konkuk.ac.kr",
    )
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="aw_b1_a@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin, org_id=org["id"])

    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "T1", -1000],
        ]
    )
    await client.post(
        f"/api/v1/settlements/{s['id']}/bank-statements",
        headers=treasurer,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    await client.post(
        f"/api/v1/settlements/{s['id']}/reconciliation:run", headers=treasurer
    )
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin)
    await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=auditor_headers,
        json={"comment": "전체 코멘트"},
    )

    bundle = await client.get(
        f"/api/v1/audit/settlements/{s['id']}", headers=auditor_headers
    )
    assert bundle.status_code == 200, bundle.text
    body = bundle.json()
    assert body["settlement"]["id"] == s["id"]
    assert len(body["bank_transactions"]) == 1
    assert len(body["reconciliation_results"]) == 1
    assert len(body["comments"]) == 1
    assert body["evidences"] == []


async def test_review_bundle_requires_auditor_role(client: AsyncClient) -> None:
    """A treasurer of the org should NOT access the auditor's review bundle —
    they have the standard `GET /settlements/{id}` route instead."""
    await signup(client, email="aw_g@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_g@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    treasurer = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        treasurer_email="aw_g_t@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin, org_id=org["id"])

    resp = await client.get(
        f"/api/v1/audit/settlements/{s['id']}", headers=treasurer
    )
    assert resp.status_code == 403, resp.text


async def test_college_auditor_can_review_other_department_settlement(
    client: AsyncClient,
) -> None:
    await signup(client, email="aw_x@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_x@konkuk.ac.kr")
    org_a = await create_org_as_admin(
        client,
        admin,
        name="컴공 학생회",
        college_name="공과대학",
        department_name="컴퓨터공학부",
    )
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org_a["id"],
        admin_headers=admin,
        auditor_email="aw_x_auditor@konkuk.ac.kr",
    )

    await signup(client, email="aw_y@konkuk.ac.kr")
    other_admin = await auth_headers(client, "aw_y@konkuk.ac.kr")
    org_b = await create_org_as_admin(
        client,
        other_admin,
        name="기계 학생회",
        college_name="공과대학",
        department_name="기계공학부",
    )
    settlement = await _create_settlement(
        client, other_admin, org_id=org_b["id"], title="기계 결산"
    )
    await client.post(
        f"/api/v1/settlements/{settlement['id']}/submit", headers=other_admin
    )

    bundle = await client.get(
        f"/api/v1/audit/settlements/{settlement['id']}",
        headers=auditor_headers,
    )
    assert bundle.status_code == 200, bundle.text

    approve = await client.post(
        f"/api/v1/settlements/{settlement['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    assert approve.status_code == 200, approve.text


async def test_review_bundle_404_for_unknown(client: AsyncClient) -> None:
    await signup(client, email="aw_404@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_404@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="aw_404_a@konkuk.ac.kr",
    )
    resp = await client.get(
        "/api/v1/audit/settlements/00000000-0000-0000-0000-000000000000",
        headers=auditor_headers,
    )
    assert resp.status_code == 404, resp.text


# --- Comment edit -------------------------------------------------------


async def test_patch_comment_author_only(client: AsyncClient) -> None:
    await signup(client, email="aw_p@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_p@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="aw_p_a@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin)

    # Auditor writes a comment, edits their own — OK.
    cm = await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=auditor_headers,
        json={"comment": "초안"},
    )
    assert cm.status_code == 201
    cid = cm.json()["id"]

    edit = await client.patch(
        f"/api/v1/audit/comments/{cid}",
        headers=auditor_headers,
        json={"comment": "수정된 코멘트"},
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["comment"] == "수정된 코멘트"

    # Admin (different user) cannot edit auditor's comment.
    bad = await client.patch(
        f"/api/v1/audit/comments/{cid}",
        headers=admin,
        json={"comment": "tamper"},
    )
    assert bad.status_code == 403, bad.text


async def test_patch_comment_unknown_id_404(client: AsyncClient) -> None:
    await signup(client, email="aw_pn@konkuk.ac.kr")
    admin = await auth_headers(client, "aw_pn@konkuk.ac.kr")
    resp = await client.patch(
        "/api/v1/audit/comments/00000000-0000-0000-0000-000000000000",
        headers=admin,
        json={"comment": "x"},
    )
    assert resp.status_code == 404, resp.text
