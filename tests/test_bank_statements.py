"""Bank statement upload + parsing tests (spec §2 Step 3).

We build a real in-memory xlsx with openpyxl so the parser actually exercises
its header detection + amount sign logic, rather than only validating the
upload extension.
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from httpx import AsyncClient
from openpyxl import Workbook

from conftest import (
    add_treasurer_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)
from union_ledger.services.bank_statement import (
    BankStatementParseError,
    _coerce_date,
    parse_bank_statement_bytes,
)


def _build_xlsx(
    rows: list[list[object]],
    *,
    leading_meta_rows: int = 0,
) -> bytes:
    """Build an xlsx with optional leading metadata rows + a header + data.

    The first row of `rows` is the header. `leading_meta_rows` empty rows
    are inserted before the header to mimic real bank exports.
    """
    wb = Workbook()
    ws = wb.active
    for _ in range(leading_meta_rows):
        ws.append([])
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
        json={"title": "거래내역 테스트", "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Upload + parse (single signed amount column) -----------------------


async def test_upload_bank_statement_amount_column(client: AsyncClient) -> None:
    await signup(client, email="bs_amt@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_amt@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "동아리 회식", -50000],
            [date(2026, 4, 5), "회비 입금", 100000],
            ["", "", ""],  # blank row should be skipped
        ]
    )
    files = {"file": ("statement.xlsx", io.BytesIO(xlsx))}
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=headers,
        files=files,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["parsed_rows_count"] == 2
    assert body["status"] == "completed"

    txs = await client.get(
        f"/api/v1/settlements/{settlement['id']}/bank-transactions",
        headers=headers,
    )
    assert txs.status_code == 200
    rows = txs.json()
    assert len(rows) == 2


async def test_upload_bank_statement_withdrawal_deposit_columns(
    client: AsyncClient,
) -> None:
    """Banks that emit separate 출금/입금 columns: withdrawals should become negative."""
    await signup(client, email="bs_wd@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_wd@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    xlsx = _build_xlsx(
        [
            ["일자", "내용", "출금", "입금"],
            [date(2026, 4, 2), "프린트 비용", 12000, None],
            [date(2026, 4, 3), "회비 입금", None, 80000],
        ],
        leading_meta_rows=2,  # banks often prepend account info rows
    )
    files = {"file": ("statement.xlsx", io.BytesIO(xlsx))}
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=headers,
        files=files,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["parsed_rows_count"] == 2

    txs = await client.get(
        f"/api/v1/settlements/{settlement['id']}/bank-transactions",
        headers=headers,
    )
    assert txs.status_code == 200
    by_desc = {row["description"]: row for row in txs.json()}
    assert float(by_desc["프린트 비용"]["amount"]) == -12000
    assert float(by_desc["회비 입금"]["amount"]) == 80000


# --- Upload errors --------------------------------------------------------


async def test_upload_rejects_unsupported_format(client: AsyncClient) -> None:
    await signup(client, email="bs_fmt@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_fmt@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    files = {"file": ("statement.csv", io.BytesIO(b"date,amount\n2026-04-01,1000"))}
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=headers,
        files=files,
    )
    assert resp.status_code == 400, resp.text


async def test_upload_rejects_empty_file(client: AsyncClient) -> None:
    await signup(client, email="bs_empty@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_empty@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    files = {"file": ("statement.xlsx", io.BytesIO(b""))}
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=headers,
        files=files,
    )
    assert resp.status_code == 400, resp.text


async def test_upload_unrecognized_header_returns_422(client: AsyncClient) -> None:
    await signup(client, email="bs_hdr@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_hdr@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    xlsx = _build_xlsx(
        [
            ["foo", "bar", "baz"],
            ["a", "b", "c"],
        ]
    )
    files = {"file": ("statement.xlsx", io.BytesIO(xlsx))}
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=headers,
        files=files,
    )
    assert resp.status_code == 422, resp.text


# --- Auth ----------------------------------------------------------------


async def test_upload_requires_treasurer_or_admin(client: AsyncClient) -> None:
    """A non-member of the target org can't upload bank statements (being
    ADMIN of their own signup-org doesn't grant cross-org perms)."""
    await signup(client, email="bs_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "bs_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])

    # Different user with no membership in the new org.
    await signup(client, email="bs_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "bs_outsider@konkuk.ac.kr")

    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "x", -1000],
        ]
    )
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=out_headers,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    assert resp.status_code == 403, resp.text


async def test_treasurer_can_upload(client: AsyncClient) -> None:
    await signup(client, email="bs_admin2@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "bs_admin2@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        treasurer_email="bs_treasurer@konkuk.ac.kr",
    )
    settlement = await _create_settlement(client, admin_headers, org_id=org["id"])

    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), "x", -1000],
        ]
    )
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=treasurer_headers,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    assert resp.status_code == 201, resp.text


async def test_upload_deposit_only_with_amount_suffixed_columns(
    client: AsyncClient,
) -> None:
    """학생회비처럼 입금만 있는 거래내역서. Columns are 출금액/입금액 (both contain
    '금액'); the deposit rows must NOT be dropped as if 출금액 were the amount."""
    await signup(client, email="bs_dep@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_dep@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "출금액", "입금액"],
            [date(2026, 4, 1), "학생회비", None, 50000],
            [date(2026, 4, 2), "학생회비", None, 30000],
        ]
    )
    resp = await client.post(
        f"/api/v1/settlements/{settlement['id']}/bank-statements",
        headers=headers,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["parsed_rows_count"] == 2

    txs = await client.get(
        f"/api/v1/settlements/{settlement['id']}/bank-transactions",
        headers=headers,
    )
    assert txs.status_code == 200
    amounts = sorted(float(row["amount"]) for row in txs.json())
    assert amounts == [30000.0, 50000.0]  # deposits kept, positive


# --- Unit tests: date coercion + legacy .xls routing ----------------------


def test_coerce_date_accepts_kb_datetime_format() -> None:
    # KB 국민은행 '거래일시' is a dotted date + time string.
    assert _coerce_date("2026.06.07 16:59:05") == date(2026, 6, 7)
    assert _coerce_date("2026/06/07 16:59") == date(2026, 6, 7)
    assert _coerce_date("2026.06.07") == date(2026, 6, 7)
    assert _coerce_date("2026-06-07 16:59:05") == date(2026, 6, 7)


def test_legacy_xls_routes_to_xlrd_reader() -> None:
    # An OLE2 header (binary .xls magic) that isn't a real workbook must be
    # handled by the xls reader (xlrd) — proving we don't hand .xls to openpyxl
    # (which would raise a different, misleading "not a zip" error).
    ole2_garbage = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128
    with pytest.raises(BankStatementParseError):
        parse_bank_statement_bytes(ole2_garbage)
