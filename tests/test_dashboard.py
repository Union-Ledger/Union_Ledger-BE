"""Dashboard tests (spec §5-1).

Treasurer dashboard verifies aggregates (evidence count, total amount,
matched/unmatched, progress%) and the recent-settlements card list.
Auditor dashboard verifies status grouping (pending/in_progress/completed)
and the pending preview list.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import async_sessionmaker

from conftest import (
    add_auditor_to_org,
    add_treasurer_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)
from union_ledger.models.entities import Evidence
from union_ledger.models.enums import EvidenceStatus, EvidenceType


def _build_xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _create_settlement(
    client: AsyncClient, headers: dict[str, str], *, org_id: str, title: str = "S"
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json={"title": title, "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed_evidence(
    db_sessionmaker: async_sessionmaker,
    *,
    settlement_id: uuid.UUID,
    organization_id: uuid.UUID,
    evidence_date: date,
    amount: Decimal,
    merchant: str,
) -> uuid.UUID:
    async with db_sessionmaker() as session:
        ev = Evidence(
            settlement_id=settlement_id,
            organization_id=organization_id,
            evidence_type=EvidenceType.PHYSICAL_RECEIPT,
            status=EvidenceStatus.NEEDS_REVIEW,
            source_file_name=f"{merchant}.png",
            source_file_path=f"/tmp/{merchant}.png",
            extracted_payload={},
            evidence_date=evidence_date,
            merchant_name=merchant,
            amount=amount,
        )
        session.add(ev)
        await session.commit()
        await session.refresh(ev)
        return ev.id


# --- Treasurer dashboard ------------------------------------------------


async def test_treasurer_dashboard_includes_admin_org(client: AsyncClient) -> None:
    """ADMIN-role memberships are counted toward the treasurer dashboard.
    Self-signup no longer creates an org, so an approved 회장 application is
    the user's single ADMIN membership → count == 1."""
    await signup(client, email="dash_t_admin@konkuk.ac.kr")
    headers = await auth_headers(client, "dash_t_admin@konkuk.ac.kr")
    await create_org_as_admin(client, headers)
    resp = await client.get("/api/v1/dashboard/treasurer", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["organization_count"] == 1


async def test_treasurer_dashboard_aggregates_across_settlements(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """Two settlements with evidences + reconciliation roll up correctly."""
    await signup(client, email="dash_t@konkuk.ac.kr")
    headers = await auth_headers(client, "dash_t@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s1 = await _create_settlement(client, headers, org_id=org["id"], title="S1")
    s2 = await _create_settlement(client, headers, org_id=org["id"], title="S2")

    sid1 = uuid.UUID(s1["id"])
    sid2 = uuid.UUID(s2["id"])
    oid = uuid.UUID(org["id"])

    # S1 has 2 evidences (10000 + 20000), S2 has 1 (5000).
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid1,
        organization_id=oid,
        evidence_date=date(2026, 4, 1),
        amount=Decimal("10000"),
        merchant="m1",
    )
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid1,
        organization_id=oid,
        evidence_date=date(2026, 4, 2),
        amount=Decimal("20000"),
        merchant="m2",
    )
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid2,
        organization_id=oid,
        evidence_date=date(2026, 4, 3),
        amount=Decimal("5000"),
        merchant="m3",
    )

    # Bank statement on S1: one matches, one doesn't → progress 50%.
    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "T1", -10000],  # matches m1
            [date(2026, 4, 2), "T2", -99999],  # amount mismatch with m2
        ]
    )
    await client.post(
        f"/api/v1/settlements/{s1['id']}/bank-statements",
        headers=headers,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    await client.post(
        f"/api/v1/settlements/{s1['id']}/reconciliation:run", headers=headers
    )

    resp = await client.get("/api/v1/dashboard/treasurer", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_evidence_count"] == 3
    assert float(body["total_evidence_amount"]) == 35000.0
    # 1 matched, 1 amount_mismatch on S1 → 1 of 2 = 50%.
    assert body["matched_count"] == 1
    assert body["unmatched_count"] == 1
    assert body["progress_percent"] == 50.0

    titles = {card["title"] for card in body["recent_settlements"]}
    assert titles == {"S1", "S2"}

    # S1's per-card view should show its progress (50%) and 2 evidences.
    s1_card = next(c for c in body["recent_settlements"] if c["title"] == "S1")
    assert s1_card["evidence_count"] == 2
    assert float(s1_card["total_evidence_amount"]) == 30000.0
    assert s1_card["progress_percent"] == 50.0
    # S2 has no reconciliation rows yet → 0%.
    s2_card = next(c for c in body["recent_settlements"] if c["title"] == "S2")
    assert s2_card["progress_percent"] == 0.0


async def test_treasurer_dashboard_excludes_orgs_without_treasurer_role(
    client: AsyncClient,
) -> None:
    """A user who is only an Auditor in some org should not see that org's
    settlements on the treasurer dashboard."""
    await signup(client, email="dash_t_x_admin@konkuk.ac.kr")
    admin = await auth_headers(client, "dash_t_x_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="dash_t_x_aud@konkuk.ac.kr",
    )
    # Admin creates a settlement; the auditor should not see it on the
    # treasurer dashboard.
    await _create_settlement(client, admin, org_id=org["id"], title="hidden")

    resp = await client.get("/api/v1/dashboard/treasurer", headers=auditor_headers)
    body = resp.json()
    titles = {card["title"] for card in body["recent_settlements"]}
    assert "hidden" not in titles


# --- Auditor dashboard --------------------------------------------------


async def test_auditor_dashboard_empty_for_non_auditor(client: AsyncClient) -> None:
    await signup(client, email="dash_a_n@konkuk.ac.kr")
    headers = await auth_headers(client, "dash_a_n@konkuk.ac.kr")
    resp = await client.get("/api/v1/dashboard/auditor", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["organization_count"] == 0
    assert body["pending_count"] == 0
    assert body["pending_settlements"] == []


async def test_auditor_dashboard_groups_status_counts(client: AsyncClient) -> None:
    """Submitted+resubmitted = pending; approved+rejected = completed."""
    await signup(client, email="dash_a@konkuk.ac.kr")
    admin = await auth_headers(client, "dash_a@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="dash_a_aud@konkuk.ac.kr",
    )
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        treasurer_email="dash_a_t@konkuk.ac.kr",
    )

    # 3 settlements: one pending (submitted), one approved, one rejected.
    pending = await _create_settlement(client, admin, org_id=org["id"], title="pending")
    approved = await _create_settlement(client, admin, org_id=org["id"], title="approved")
    rejected = await _create_settlement(client, admin, org_id=org["id"], title="rejected")

    await client.post(f"/api/v1/settlements/{pending['id']}/submit", headers=admin)
    await client.post(f"/api/v1/settlements/{approved['id']}/submit", headers=admin)
    await client.post(
        f"/api/v1/settlements/{approved['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    await client.post(f"/api/v1/settlements/{rejected['id']}/submit", headers=admin)
    await client.post(
        f"/api/v1/settlements/{rejected['id']}/audit/reject",
        headers=auditor_headers,
        json={},
    )

    resp = await client.get("/api/v1/dashboard/auditor", headers=auditor_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_count"] == 1
    assert body["pending_count"] == 1
    assert body["completed_count"] == 2  # approved + rejected
    titles = {item["title"] for item in body["pending_settlements"]}
    assert titles == {"pending"}

    # Treasurer's auditor dashboard should be empty (they have no auditor role).
    treasurer_resp = await client.get(
        "/api/v1/dashboard/auditor", headers=treasurer_headers
    )
    assert treasurer_resp.json()["organization_count"] == 0


async def test_auditor_dashboard_pending_limit(client: AsyncClient) -> None:
    await signup(client, email="dash_a_l@konkuk.ac.kr")
    admin = await auth_headers(client, "dash_a_l@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="dash_a_l_aud@konkuk.ac.kr",
    )
    for i in range(3):
        s = await _create_settlement(client, admin, org_id=org["id"], title=f"S{i}")
        await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin)

    resp = await client.get(
        "/api/v1/dashboard/auditor?pending_limit=2", headers=auditor_headers
    )
    body = resp.json()
    assert body["pending_count"] == 3  # full count
    assert len(body["pending_settlements"]) == 2  # truncated preview


async def test_auditor_dashboard_includes_comment_count(client: AsyncClient) -> None:
    """The pending preview surfaces the real audit comment count, not a
    hardcoded 0 (it must agree with the /audit/settlements worklist)."""
    await signup(client, email="dash_a_cm@konkuk.ac.kr")
    admin = await auth_headers(client, "dash_a_cm@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="dash_a_cm_aud@konkuk.ac.kr",
    )
    settlement = await _create_settlement(
        client, admin, org_id=org["id"], title="commented"
    )
    await client.post(f"/api/v1/settlements/{settlement['id']}/submit", headers=admin)
    for text in ("확인 필요", "금액 재확인"):
        cm = await client.post(
            f"/api/v1/settlements/{settlement['id']}/comments",
            headers=auditor_headers,
            json={"comment": text},
        )
        assert cm.status_code == 201, cm.text

    resp = await client.get("/api/v1/dashboard/auditor", headers=auditor_headers)
    assert resp.status_code == 200, resp.text
    card = next(
        c for c in resp.json()["pending_settlements"] if c["title"] == "commented"
    )
    assert card["audit_comment_count"] == 2


# --- President dashboard ------------------------------------------------


async def test_president_dashboard_rolls_up_team(client: AsyncClient) -> None:
    await signup(client, email="pres_admin@konkuk.ac.kr", name="홍회장")
    admin = await auth_headers(client, "pres_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="pres_aud@konkuk.ac.kr",
    )
    await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        treasurer_email="pres_t@konkuk.ac.kr",
    )

    approved = await _create_settlement(
        client, admin, org_id=org["id"], title="approved"
    )
    pending = await _create_settlement(
        client, admin, org_id=org["id"], title="pending"
    )

    await client.post(f"/api/v1/settlements/{approved['id']}/submit", headers=admin)
    await client.post(
        f"/api/v1/settlements/{approved['id']}/audit/approve",
        headers=auditor_headers,
        json={"comment": "확인 완료"},
    )
    await client.post(f"/api/v1/settlements/{pending['id']}/submit", headers=admin)

    resp = await client.get(
        f"/api/v1/dashboard/president?organization_id={org['id']}", headers=admin
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["team_member_count"] == 3  # admin + treasurer + auditor
    assert body["submitted_settlement_count"] == 2  # both submitted
    assert body["audit_completed_count"] == 1  # approved
    assert body["review_pending_count"] == 1  # still-submitted one

    assert body["organization"]["college_name"] == "공과대학"
    assert body["organization"]["president_name"] == "홍회장"
    assert body["organization"]["current_period_label"] == "2026-1학기"

    titles = {c["title"] for c in body["treasurer_work"]}
    assert titles == {"approved", "pending"}

    roles = {m["role"] for m in body["members"]}
    assert {"president", "treasurer", "auditor"} <= roles

    # The auditor commented on the approved settlement → counted as completed.
    assert len(body["auditor_activity"]) == 1
    card = body["auditor_activity"][0]
    assert card["email"] == "pres_aud@konkuk.ac.kr"
    assert card["completed_count"] == 1


async def test_president_dashboard_defaults_to_president_org(client: AsyncClient) -> None:
    # No organization_id → resolves the caller's single PRESIDENT org.
    await signup(client, email="pres_default@konkuk.ac.kr", name="기본회장")
    headers = await auth_headers(client, "pres_default@konkuk.ac.kr")
    await create_org_as_admin(client, headers, name="기본조직")
    resp = await client.get("/api/v1/dashboard/president", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["organization"]["president_name"] == "기본회장"


async def test_president_dashboard_rejects_non_admin(client: AsyncClient) -> None:
    await signup(client, email="pres_owner@konkuk.ac.kr")
    admin = await auth_headers(client, "pres_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        treasurer_email="pres_not_admin@konkuk.ac.kr",
    )

    resp = await client.get(
        f"/api/v1/dashboard/president?organization_id={org['id']}",
        headers=treasurer_headers,
    )
    assert resp.status_code == 403, resp.text
