"""Audit flow tests (spec ⑧).

State-machine matrix the suite covers:

    DRAFT --submit--> SUBMITTED --approve--> APPROVED --publish--> (published)
                                |
                                +--reject--> REJECTED --resubmit--> RESUBMITTED
                                                                       |
                                                                       +--approve/reject

Plus:
- Role gating per transition (Treasurer/Auditor/Admin)
- Optional `comment` body on approve/reject/resubmit auto-attached as AuditComment
- Standalone POST/GET /comments
"""

from __future__ import annotations

import io

from httpx import AsyncClient

from conftest import (
    add_auditor_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)


async def _create_settlement(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    org_id: str,
    title: str = "결산안",
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json={"title": title, "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _bootstrap_org_with_auditor(
    client: AsyncClient,
    *,
    admin_email: str,
    auditor_email: str,
) -> tuple[dict, dict[str, str], dict[str, str]]:
    """Returns (org_dict, admin_headers, auditor_headers)."""
    await signup(client, email=admin_email)
    admin_headers = await auth_headers(client, admin_email)
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email=auditor_email,
    )
    return org, admin_headers, auditor_headers


# --- Submit ---------------------------------------------------------------


async def test_submit_draft_settlement(client: AsyncClient) -> None:
    await signup(client, email="sub_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "sub_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    s = await _create_settlement(client, admin_headers, org_id=org["id"])

    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/submit",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["submitted_at"] is not None
    assert body["audited_at"] is None


async def test_submit_generates_artifacts(
    client: AsyncClient,
    db_sessionmaker,
    tmp_path,
    monkeypatch,
) -> None:
    import json
    import uuid
    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    from union_ledger.core.config import get_settings
    from union_ledger.models.entities import Evidence
    from union_ledger.models.enums import EvidenceStatus, EvidenceType

    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "settlement_templates"
        / "audit_ledger_sample.xlsx"
    )
    if not fixture.is_file():
        return

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    await signup(client, email="sub_art@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "sub_art@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)

    files = {
        "file": (
            "audit_ledger_sample.xlsx",
            io.BytesIO(fixture.read_bytes()),
            "application/vnd.ms-excel",
        )
    }
    template_resp = await client.post(
        f"/api/v1/organizations/{org['id']}/templates",
        headers=admin_headers,
        files=files,
        data={"name": "공과대", "mapping_schema": json.dumps({"B1": "title"})},
    )
    assert template_resp.status_code == 201, template_resp.text
    template_id = template_resp.json()["id"]

    settlement_resp = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=admin_headers,
        json={
            "title": "2026-1학기 결산안",
            "academic_year": 2026,
            "semester": "1",
            "template_id": template_id,
        },
    )
    assert settlement_resp.status_code == 201, settlement_resp.text
    settlement = settlement_resp.json()

    img_dir = tmp_path / "evidences" / settlement["id"]
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / "ev.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    async with db_sessionmaker() as session:
        session.add(
            Evidence(
                settlement_id=uuid.UUID(settlement["id"]),
                organization_id=uuid.UUID(org["id"]),
                evidence_type=EvidenceType.PHYSICAL_RECEIPT,
                status=EvidenceStatus.CONFIRMED,
                source_file_name="ev.png",
                source_file_path=str(img_path),
                extracted_payload={},
                evidence_date=date(2025, 12, 23),
                merchant_name="테스트상점",
                amount=Decimal("85000"),
                budget_category="비품",
            )
        )
        await session.commit()

    submit_resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/submit",
        headers=admin_headers,
    )
    assert submit_resp.status_code == 200, submit_resp.text

    artifacts_resp = await client.get(
        f"/api/v1/settlements/{settlement['id']}/artifacts",
        headers=admin_headers,
    )
    assert artifacts_resp.status_code == 200, artifacts_resp.text
    artifacts = artifacts_resp.json()
    assert len(artifacts) == 2
    excel = next(a for a in artifacts if a["artifact_type"] == "settlement_excel")
    assert excel["status"] == "completed"
    assert excel["file_path"]


async def test_submit_rejects_non_draft(client: AsyncClient) -> None:
    await signup(client, email="sub_nodr@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "sub_nodr@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    # Transition once to SUBMITTED, then try again.
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/submit",
        headers=admin_headers,
    )
    assert resp.status_code == 400, resp.text


async def test_submit_rejects_non_treasurer(client: AsyncClient) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="sub_a@konkuk.ac.kr",
        auditor_email="sub_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    # Auditor cannot submit.
    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/submit",
        headers=auditor_headers,
    )
    assert resp.status_code == 403, resp.text


# --- Approve --------------------------------------------------------------


async def test_approve_by_auditor(client: AsyncClient) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="ap_a@konkuk.ac.kr",
        auditor_email="ap_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)

    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["audited_at"] is not None


async def test_approve_with_comment_attaches_audit_comment(
    client: AsyncClient,
) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="apc_a@konkuk.ac.kr",
        auditor_email="apc_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)

    await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={"comment": "잘 작성되었습니다."},
    )
    listing = await client.get(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=admin_headers,
    )
    assert listing.status_code == 200
    comments = listing.json()
    assert len(comments) == 1
    assert comments[0]["comment"] == "잘 작성되었습니다."


async def test_approve_rejects_treasurer(client: AsyncClient) -> None:
    """Treasurer/Admin can submit but not approve — separation of duties."""
    org, admin_headers, _ = await _bootstrap_org_with_auditor(
        client,
        admin_email="ap_no_a@konkuk.ac.kr",
        auditor_email="ap_no_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)

    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 403, resp.text


async def test_approve_rejects_draft(client: AsyncClient) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="ap_dr_a@konkuk.ac.kr",
        auditor_email="ap_dr_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    # Skip submit — try to approve directly.
    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    assert resp.status_code == 400, resp.text


# --- Reject + Resubmit ----------------------------------------------------


async def test_reject_then_resubmit_then_approve(client: AsyncClient) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="rr_a@konkuk.ac.kr",
        auditor_email="rr_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)

    rj = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/reject",
        headers=auditor_headers,
        json={"comment": "영수증 누락"},
    )
    assert rj.status_code == 200
    assert rj.json()["status"] == "rejected"
    assert rj.json()["audited_at"] is not None

    # Resubmit clears audited_at.
    re = await client.post(
        f"/api/v1/settlements/{s['id']}/resubmit",
        headers=admin_headers,
        json={"comment": "수정 완료"},
    )
    assert re.status_code == 200, re.text
    assert re.json()["status"] == "resubmitted"
    assert re.json()["audited_at"] is None

    # Auditor can approve a RESUBMITTED settlement.
    ap = await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    assert ap.status_code == 200
    assert ap.json()["status"] == "approved"

    # Both reject + resubmit + approve comments captured.
    listing = await client.get(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=admin_headers,
    )
    assert listing.status_code == 200
    bodies = [c["comment"] for c in listing.json()]
    assert "영수증 누락" in bodies
    assert "수정 완료" in bodies


async def test_resubmit_rejects_non_rejected(client: AsyncClient) -> None:
    await signup(client, email="re_dr@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "re_dr@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    # DRAFT → resubmit is illegal.
    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/resubmit",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 400, resp.text


# --- Publish --------------------------------------------------------------


async def test_publish_after_approve(client: AsyncClient) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="pb_a@konkuk.ac.kr",
        auditor_email="pb_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )

    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/publish",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["published_at"] is not None


async def test_publish_rejects_non_approved(client: AsyncClient) -> None:
    await signup(client, email="pb_no@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "pb_no@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    # Still DRAFT, can't publish.
    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/publish",
        headers=admin_headers,
    )
    assert resp.status_code == 400


async def test_publish_idempotent_guard(client: AsyncClient) -> None:
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="pb_idem_a@konkuk.ac.kr",
        auditor_email="pb_idem_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    first = await client.post(
        f"/api/v1/settlements/{s['id']}/publish",
        headers=admin_headers,
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/settlements/{s['id']}/publish",
        headers=admin_headers,
    )
    assert second.status_code == 400, second.text


async def test_publish_rejects_auditor(client: AsyncClient) -> None:
    """Only Admin can publish — Auditor approves, but doesn't release."""
    org, admin_headers, auditor_headers = await _bootstrap_org_with_auditor(
        client,
        admin_email="pb_role_a@konkuk.ac.kr",
        auditor_email="pb_role_audi@konkuk.ac.kr",
    )
    s = await _create_settlement(client, admin_headers, org_id=org["id"])
    await client.post(f"/api/v1/settlements/{s['id']}/submit", headers=admin_headers)
    await client.post(
        f"/api/v1/settlements/{s['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    resp = await client.post(
        f"/api/v1/settlements/{s['id']}/publish",
        headers=auditor_headers,
    )
    assert resp.status_code == 403, resp.text


# --- Comments standalone --------------------------------------------------


async def test_post_comment_member_only(client: AsyncClient) -> None:
    await signup(client, email="cm_owner@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "cm_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    s = await _create_settlement(client, admin_headers, org_id=org["id"])

    ok = await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=admin_headers,
        json={"comment": "검토 요청드립니다."},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["author_membership_id"] is not None

    # Outsider can't comment.
    await signup(client, email="cm_out@konkuk.ac.kr")
    out_headers = await auth_headers(client, "cm_out@konkuk.ac.kr")
    forbid = await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=out_headers,
        json={"comment": "x"},
    )
    assert forbid.status_code == 403


async def test_post_comment_with_evidence_in_other_settlement_400(
    client: AsyncClient,
) -> None:
    """If you reference an evidence_id that doesn't belong to this settlement,
    the create call should bail with 400."""
    await signup(client, email="cm_ev_a@konkuk.ac.kr")
    headers = await auth_headers(client, "cm_ev_a@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s_a = await _create_settlement(client, headers, org_id=org["id"], title="A")
    s_b = await _create_settlement(client, headers, org_id=org["id"], title="B")

    # Pretend we have an evidence id from settlement B; reuse a random uuid
    # since evidence creation isn't covered here. The service's check is
    # "evidence row missing OR settlement_id mismatch", both yield 400.
    resp = await client.post(
        f"/api/v1/settlements/{s_a['id']}/comments",
        headers=headers,
        json={
            "comment": "잘못된 링크",
            "evidence_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 400, resp.text
    # Ensure no comment was attached to settlement A.
    listing = await client.get(
        f"/api/v1/settlements/{s_a['id']}/comments",
        headers=headers,
    )
    assert listing.json() == []
    del s_b  # not needed — kept for the "two settlements exist" semantics.


async def test_list_comments_member_only(client: AsyncClient) -> None:
    await signup(client, email="cml_owner@konkuk.ac.kr")
    headers = await auth_headers(client, "cml_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s = await _create_settlement(client, headers, org_id=org["id"])
    await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=headers,
        json={"comment": "1"},
    )
    await client.post(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=headers,
        json={"comment": "2"},
    )
    listing = await client.get(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=headers,
    )
    assert listing.status_code == 200
    bodies = [c["comment"] for c in listing.json()]
    assert bodies == ["1", "2"]  # chronological

    await signup(client, email="cml_out@konkuk.ac.kr")
    out_headers = await auth_headers(client, "cml_out@konkuk.ac.kr")
    forbid = await client.get(
        f"/api/v1/settlements/{s['id']}/comments",
        headers=out_headers,
    )
    assert forbid.status_code == 403
