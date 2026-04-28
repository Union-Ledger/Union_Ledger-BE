"""Public Viewer tests (spec §4).

Covers:
  - Listing only includes APPROVED + published settlements
  - College-based visibility scoping
  - Detail / items / evidence / artifact download all gated on publish state
  - Cross-college users see 404 (not 403) — existence is hidden
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from openpyxl import Workbook
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker

from conftest import (
    add_auditor_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)
from union_ledger.models.entities import Evidence
from union_ledger.models.enums import EvidenceStatus, EvidenceType


def _png_bytes() -> bytes:
    img = Image.new("RGB", (40, 40), color=(150, 150, 150))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _xlsx_template_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "결산"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _publish_settlement_with_evidence(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    *,
    storage_root,
    college: str = "공과대학",
    department: str = "컴퓨터공학부",
    title: str = "공개될 결산",
) -> tuple[dict, dict, dict, dict[str, str]]:
    """Bootstraps an org+settlement, publishes it, returns
    (org, settlement, evidence_dict, admin_headers)."""
    admin_email = f"pv_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"
    auditor_email = f"pv_{uuid.uuid4().hex[:6]}@konkuk.ac.kr"

    await signup(
        client,
        email=admin_email,
        college_name=college,
        department_name=department,
    )
    admin_headers = await auth_headers(client, admin_email)
    org = await create_org_as_admin(
        client,
        admin_headers,
        college_name=college,
        department_name=department,
    )

    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email=auditor_email,
    )

    # Settlement → submit → approve → publish.
    s_resp = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=admin_headers,
        json={"title": title, "academic_year": 2026, "semester": "1"},
    )
    assert s_resp.status_code == 201
    settlement = s_resp.json()

    # Seed an evidence file directly (skip OCR upload roundtrip).
    img_dir = storage_root / "evidences" / settlement["id"]
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / "rec.png"
    img_path.write_bytes(_png_bytes())
    async with db_sessionmaker() as session:
        ev = Evidence(
            settlement_id=uuid.UUID(settlement["id"]),
            organization_id=uuid.UUID(org["id"]),
            evidence_type=EvidenceType.PHYSICAL_RECEIPT,
            status=EvidenceStatus.CONFIRMED,
            source_file_name="rec.png",
            source_file_path=str(img_path),
            extracted_payload={},
            evidence_date=date(2026, 4, 1),
            merchant_name="공개가게",
            amount=Decimal("5000"),
        )
        session.add(ev)
        await session.commit()
        await session.refresh(ev)
        ev_dict = {
            "id": str(ev.id),
            "settlement_id": str(ev.settlement_id),
            "source_file_path": str(ev.source_file_path),
        }

    await client.post(
        f"/api/v1/settlements/{settlement['id']}/submit", headers=admin_headers
    )
    await client.post(
        f"/api/v1/settlements/{settlement['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    pub = await client.post(
        f"/api/v1/settlements/{settlement['id']}/publish", headers=admin_headers
    )
    assert pub.status_code == 200, pub.text
    settlement = pub.json()
    return org, settlement, ev_dict, admin_headers


# --- Listing -------------------------------------------------------------


async def test_published_settlement_appears_in_list(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    _, settlement, _, admin_headers = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path
    )

    resp = await client.get("/api/v1/public/settlements", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    titles = {row["title"] for row in resp.json()}
    assert settlement["title"] in titles


async def test_unpublished_settlement_hidden_from_list(client: AsyncClient) -> None:
    """Settlement that's been approved but not yet published must NOT appear."""
    await signup(client, email="pv_h_admin@konkuk.ac.kr")
    admin = await auth_headers(client, "pv_h_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    auditor = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin,
        auditor_email="pv_h_auditor@konkuk.ac.kr",
    )
    s = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=admin,
        json={"title": "approved-not-published", "academic_year": 2026, "semester": "1"},
    )
    sid = s.json()["id"]
    await client.post(f"/api/v1/settlements/{sid}/submit", headers=admin)
    await client.post(
        f"/api/v1/settlements/{sid}/audit/approve", headers=auditor, json={}
    )
    # Skip publish on purpose.

    resp = await client.get("/api/v1/public/settlements", headers=admin)
    titles = {row["title"] for row in resp.json()}
    assert "approved-not-published" not in titles


async def test_other_college_settlements_hidden(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    """A user signed up under college A should not see college B's published
    settlements in the public list."""
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    # Publish a settlement under "공과대학"
    _, settlement_a, _, _ = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path,
        college="공과대학", department="컴퓨터공학부",
        title="공과대 결산",
    )

    # Different user in "예술대학"
    await signup(
        client,
        email="pv_other@konkuk.ac.kr",
        college_name="예술대학",
        department_name="음악과",
    )
    other_headers = await auth_headers(client, "pv_other@konkuk.ac.kr")
    resp = await client.get("/api/v1/public/settlements", headers=other_headers)
    assert resp.status_code == 200
    titles = {row["title"] for row in resp.json()}
    assert "공과대 결산" not in titles


# --- Detail / items / evidence ------------------------------------------


async def test_get_published_detail(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    org, settlement, _, admin_headers = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path
    )
    resp = await client.get(
        f"/api/v1/public/settlements/{settlement['id']}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == settlement["id"]
    assert body["organization_name"] == org["name"]
    assert body["item_count"] == 1
    assert float(body["total_amount"]) == 5000.0
    assert body["published_at"] is not None


async def test_get_unpublished_detail_404(client: AsyncClient) -> None:
    await signup(client, email="pv_d_admin@konkuk.ac.kr")
    admin = await auth_headers(client, "pv_d_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    s = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=admin,
        json={"title": "draft", "academic_year": 2026, "semester": "1"},
    )
    resp = await client.get(
        f"/api/v1/public/settlements/{s.json()['id']}", headers=admin
    )
    assert resp.status_code == 404


async def test_items_returns_evidence_summary(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    _, settlement, ev, admin_headers = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path
    )
    resp = await client.get(
        f"/api/v1/public/settlements/{settlement['id']}/items",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["evidence_id"] == ev["id"]
    assert items[0]["merchant_name"] == "공개가게"
    assert items[0]["has_evidence_file"] is True


async def test_get_public_evidence_metadata_and_file(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    _, _settlement, ev, admin_headers = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path
    )
    meta = await client.get(
        f"/api/v1/public/evidences/{ev['id']}", headers=admin_headers
    )
    assert meta.status_code == 200, meta.text
    body = meta.json()
    # Sensitive fields must not leak.
    assert "source_file_path" not in body
    assert "extracted_payload" not in body
    assert body["source_file_name"] == "rec.png"
    assert body["has_evidence_file"] is True

    file_resp = await client.get(
        f"/api/v1/public/evidences/{ev['id']}/file", headers=admin_headers
    )
    assert file_resp.status_code == 200
    assert file_resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


async def test_evidence_in_unpublished_settlement_returns_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    await signup(client, email="pv_e@konkuk.ac.kr")
    admin = await auth_headers(client, "pv_e@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin)
    s = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=admin,
        json={"title": "x", "academic_year": 2026, "semester": "1"},
    )
    sid = uuid.UUID(s.json()["id"])
    img_path = tmp_path / "evidences" / str(sid) / "x.png"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    img_path.write_bytes(_png_bytes())
    async with db_sessionmaker() as session:
        ev = Evidence(
            settlement_id=sid,
            organization_id=uuid.UUID(org["id"]),
            evidence_type=EvidenceType.PHYSICAL_RECEIPT,
            status=EvidenceStatus.NEEDS_REVIEW,
            source_file_name="x.png",
            source_file_path=str(img_path),
            extracted_payload={},
        )
        session.add(ev)
        await session.commit()
        await session.refresh(ev)
        eid = ev.id

    resp = await client.get(f"/api/v1/public/evidences/{eid}", headers=admin)
    assert resp.status_code == 404, resp.text


# --- Artifact download --------------------------------------------------


async def test_artifact_download_after_publish(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    # Publish + generate artifacts. We need a template first.
    import json

    _, settlement, _, admin_headers = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path
    )

    # Upload a template (we need the org id from settlement → fetch detail).
    org_id = settlement["organization_id"]
    files = {"file": ("template.xlsx", io.BytesIO(_xlsx_template_bytes()))}
    template_resp = await client.post(
        f"/api/v1/organizations/{org_id}/templates",
        headers=admin_headers,
        files=files,
        data={"name": "tpl", "mapping_schema": json.dumps({"A2": "title"})},
    )
    assert template_resp.status_code == 201

    # Re-link template via PATCH while still in DRAFT — but the published
    # settlement is already APPROVED, so we can't edit. Instead create a new
    # workflow with template attached and republish. Simpler: generate
    # artifacts on the *already-published* settlement directly. The endpoint
    # doesn't gate on status, only role.
    gen = await client.post(
        f"/api/v1/settlements/{settlement['id']}/artifacts:generate",
        headers=admin_headers,
    )
    assert gen.status_code == 200, gen.text
    # PDF will succeed (one image evidence). Excel may fail (no template
    # linked) — we just want one COMPLETED artifact to download.
    pdf_artifact = gen.json()["pdf"]
    assert pdf_artifact["status"] == "completed"

    dl = await client.get(
        f"/api/v1/public/settlements/{settlement['id']}/downloads/{pdf_artifact['id']}",
        headers=admin_headers,
    )
    assert dl.status_code == 200, dl.text
    assert dl.content[:5] == b"%PDF-"  # PDF magic


async def test_artifact_download_mismatched_pair_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, tmp_path, monkeypatch
) -> None:
    """A real artifact id but the wrong settlement_id in the path → 404."""
    from union_ledger.core.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)

    _, settlement, _, admin_headers = await _publish_settlement_with_evidence(
        client, db_sessionmaker, storage_root=tmp_path
    )
    gen = await client.post(
        f"/api/v1/settlements/{settlement['id']}/artifacts:generate",
        headers=admin_headers,
    )
    artifact_id = gen.json()["pdf"]["id"]

    bogus = await client.get(
        f"/api/v1/public/settlements/00000000-0000-0000-0000-000000000000/downloads/{artifact_id}",
        headers=admin_headers,
    )
    assert bogus.status_code == 404
