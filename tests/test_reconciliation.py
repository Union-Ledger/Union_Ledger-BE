"""Reconciliation tests (spec §2 Step 3–4).

Builds a settlement with a few evidences and a parsed bank statement, then
checks that the auto-match algorithm classifies each row into MATCHED /
AMOUNT_MISMATCH / DATE_MISMATCH / MISSING_BANK_TRANSACTION / MISSING_EVIDENCE.

The evidence rows are seeded directly via the SQLAlchemy session (the
`PATCH /evidences/{id}` endpoint exists, but for setup we want to skip the
upload-and-OCR roundtrip and write fields directly).
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import async_sessionmaker

from conftest import auth_headers, create_org_as_admin, signup
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
    client: AsyncClient, headers: dict[str, str], *, org_id: str
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json={"title": "대조 테스트", "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201
    return resp.json()


async def _seed_evidence(
    db_sessionmaker: async_sessionmaker,
    *,
    settlement_id: uuid.UUID,
    organization_id: uuid.UUID,
    evidence_date: date,
    amount: Decimal,
    merchant: str,
    status: EvidenceStatus = EvidenceStatus.NEEDS_REVIEW,
) -> uuid.UUID:
    async with db_sessionmaker() as session:
        ev = Evidence(
            settlement_id=settlement_id,
            organization_id=organization_id,
            evidence_type=EvidenceType.PHYSICAL_RECEIPT,
            status=status,
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


async def _upload_bank_statement(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    settlement_id: str,
    rows: list[list[object]],
) -> None:
    xlsx = _build_xlsx(rows)
    resp = await client.post(
        f"/api/v1/settlements/{settlement_id}/bank-statements",
        headers=headers,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    assert resp.status_code == 201, resp.text


# --- Auto-match -----------------------------------------------------------


async def test_auto_match_exact(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await signup(client, email="rc_exact@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_exact@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(settlement["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 4, 1),
        amount=Decimal("50000"),
        merchant="회식집",
    )
    await _upload_bank_statement(
        client,
        headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "회식", -50000],
        ],
    )

    run = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["matched"] == 1
    assert body["amount_mismatch"] == 0
    assert body["date_mismatch"] == 0
    assert body["missing_bank_transaction"] == 0
    assert body["missing_evidence"] == 0
    assert body["total"] == 1


async def test_auto_match_classifies_partials_and_missing(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await signup(client, email="rc_mix@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_mix@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    sid = uuid.UUID(settlement["id"])
    oid = uuid.UUID(org["id"])

    # E1: exact match
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid,
        organization_id=oid,
        evidence_date=date(2026, 4, 1),
        amount=Decimal("10000"),
        merchant="exact",
    )
    # E2: same date, different amount → AMOUNT_MISMATCH
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid,
        organization_id=oid,
        evidence_date=date(2026, 4, 2),
        amount=Decimal("20000"),
        merchant="amount-mismatch",
    )
    # E3: same amount, different date → DATE_MISMATCH
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid,
        organization_id=oid,
        evidence_date=date(2026, 4, 10),
        amount=Decimal("30000"),
        merchant="date-mismatch",
    )
    # E4: nothing matches → MISSING_BANK_TRANSACTION
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=sid,
        organization_id=oid,
        evidence_date=date(2026, 4, 20),
        amount=Decimal("99999"),
        merchant="lonely-evidence",
    )

    await _upload_bank_statement(
        client,
        headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            # T1 ↔ E1 exact
            [date(2026, 4, 1), "T1", -10000],
            # T2 ↔ E2 same date, different amount
            [date(2026, 4, 2), "T2", -25000],
            # T3 ↔ E3 same amount, different date
            [date(2026, 4, 9), "T3", -30000],
            # T4: nothing matches → MISSING_EVIDENCE
            [date(2026, 4, 30), "T4", -77777],
        ],
    )

    run = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["matched"] == 1
    assert body["amount_mismatch"] == 1
    assert body["date_mismatch"] == 1
    assert body["missing_bank_transaction"] == 1
    assert body["missing_evidence"] == 1
    assert body["total"] == 5


async def test_run_is_idempotent(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """Re-running should not duplicate rows."""
    await signup(client, email="rc_idem@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_idem@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(settlement["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 4, 1),
        amount=Decimal("5000"),
        merchant="x",
    )
    await _upload_bank_statement(
        client,
        headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "x", -5000],
        ],
    )

    first = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )
    second = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["total"] == 1
    assert first.json()["total"] == 1


# --- List + filter --------------------------------------------------------


async def test_list_results_with_status_filter(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await signup(client, email="rc_list@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_list@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(settlement["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 4, 1),
        amount=Decimal("1000"),
        merchant="e1",
    )
    await _upload_bank_statement(
        client,
        headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "x", -1000],
            [date(2026, 4, 2), "extra", -2000],  # MISSING_EVIDENCE
        ],
    )
    await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )

    matched_only = await client.get(
        f"/api/v1/settlements/{settlement['id']}/reconciliation?status=matched",
        headers=headers,
    )
    assert matched_only.status_code == 200
    matched_rows = matched_only.json()
    assert len(matched_rows) == 1
    assert matched_rows[0]["status"] == "matched"


# --- Manual override ------------------------------------------------------


async def test_patch_match_manual_resolution(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await signup(client, email="rc_patch@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_patch@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(settlement["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 4, 1),
        amount=Decimal("12345"),
        merchant="lonely",
    )
    await _upload_bank_statement(
        client,
        headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 5, 1), "unrelated", -99999],
        ],
    )
    run = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )
    rows = run.json()["results"]
    missing_bank = next(
        r for r in rows if r["status"] == "missing_bank_transaction"
    )

    patch_resp = await client.patch(
        f"/api/v1/reconciliation/{missing_bank['id']}",
        headers=headers,
        json={
            "status": "manually_resolved",
            "notes": "현금 결제로 은행 기록 없음",
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text
    body = patch_resp.json()
    assert body["status"] == "manually_resolved"
    assert body["notes"] == "현금 결제로 은행 기록 없음"


async def test_patch_match_rejects_cross_settlement_evidence(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """Evidence from a *different* settlement must not be linkable."""
    await signup(client, email="rc_xs@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_xs@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    s1 = await _create_settlement(client, headers, org_id=org["id"])
    # Make a separate settlement with its own evidence row.
    s2_resp = await client.post(
        f"/api/v1/organizations/{org['id']}/settlements",
        headers=headers,
        json={"title": "s2", "academic_year": 2026, "semester": "2"},
    )
    s2 = s2_resp.json()
    foreign_evidence_id = await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(s2["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 9, 1),
        amount=Decimal("777"),
        merchant="other-settlement",
    )

    # Build a MISSING_EVIDENCE row in s1.
    await _upload_bank_statement(
        client,
        headers,
        settlement_id=s1["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "lonely", -1000],
        ],
    )
    run = await client.post(
        f"/api/v1/settlements/{s1['id']}/reconciliation:run", headers=headers
    )
    missing_ev = next(
        r for r in run.json()["results"] if r["status"] == "missing_evidence"
    )

    bad = await client.patch(
        f"/api/v1/reconciliation/{missing_ev['id']}",
        headers=headers,
        json={"evidence_id": str(foreign_evidence_id)},
    )
    assert bad.status_code == 400, bad.text


async def test_patch_match_rejects_outsider(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await signup(client, email="rc_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "rc_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])
    await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(settlement["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 4, 1),
        amount=Decimal("1000"),
        merchant="x",
    )
    await _upload_bank_statement(
        client,
        owner_headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "x", -1000],
        ],
    )
    run = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=owner_headers,
    )
    match_id = run.json()["results"][0]["id"]

    await signup(client, email="rc_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "rc_outsider@konkuk.ac.kr")
    resp = await client.patch(
        f"/api/v1/reconciliation/{match_id}",
        headers=out_headers,
        json={"notes": "hacked"},
    )
    assert resp.status_code == 403, resp.text


async def test_same_date_wildly_different_amounts_are_not_soft_paired(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """3700원 증빙과 300000원 입금이 같은 날이어도 억지로 금액 불일치로 묶지 않음."""
    await signup(client, email="rc_gap@konkuk.ac.kr")
    headers = await auth_headers(client, "rc_gap@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    await _seed_evidence(
        db_sessionmaker,
        settlement_id=uuid.UUID(settlement["id"]),
        organization_id=uuid.UUID(org["id"]),
        evidence_date=date(2026, 6, 9),
        amount=Decimal("3700"),
        merchant="카페",
    )
    await _upload_bank_statement(
        client,
        headers,
        settlement_id=settlement["id"],
        rows=[
            ["거래일자", "적요", "금액"],
            [date(2026, 6, 9), "모임회비", 300000],
        ],
    )

    run = await client.post(
        f"/api/v1/settlements/{settlement['id']}/reconciliation:run",
        headers=headers,
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["matched"] == 0
    assert body["amount_mismatch"] == 0
    assert body["missing_bank_transaction"] == 1
    assert body["missing_evidence"] == 1
