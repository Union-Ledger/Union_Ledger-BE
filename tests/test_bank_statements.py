"""Bank statement upload + parsing tests (spec §2 Step 3).

We build a real in-memory xlsx with openpyxl so the parser actually exercises
its header detection + amount sign logic, rather than only validating the
upload extension.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

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


def _build_strict_ooxml_xlsx(rows: list[list[object]]) -> bytes:
    """Build a minimal strict-namespace xlsx that exercises the XML fallback."""
    strict_ns = "http://purl.oclc.org/ooxml/spreadsheetml/main"

    def col_name(index: int) -> str:
        name = ""
        while index >= 0:
            index, rem = divmod(index, 26)
            name = chr(ord("A") + rem) + name
            index -= 1
        return name

    def cell_xml(row_idx: int, col_idx: int, value: object) -> str:
        ref = f"{col_name(col_idx)}{row_idx}"
        if isinstance(value, (int, float, Decimal)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if value is None:
            return f'<c r="{ref}"/>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'

    sheet_rows = "\n".join(
        f'<row r="{row_idx}">'
        + "".join(cell_xml(row_idx, col_idx, value) for col_idx, value in enumerate(row))
        + "</row>"
        for row_idx, row in enumerate(rows, start=1)
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels"
    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{strict_ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{strict_ns}">
  <sheetData>
    {sheet_rows}
  </sheetData>
</worksheet>""",
        )
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


# --- Delete upload --------------------------------------------------------


async def _upload_simple_statement(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    settlement_id: str,
    description: str,
    amount: int,
) -> dict:
    xlsx = _build_xlsx(
        [
            ["거래일자", "적요", "금액"],
            [date(2026, 4, 1), description, amount],
        ]
    )
    resp = await client.post(
        f"/api/v1/settlements/{settlement_id}/bank-statements",
        headers=headers,
        files={"file": ("statement.xlsx", io.BytesIO(xlsx))},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_delete_upload_removes_only_that_files_transactions(
    client: AsyncClient,
) -> None:
    await signup(client, email="bs_del@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_del@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])
    sid = settlement["id"]

    first = await _upload_simple_statement(
        client, headers, settlement_id=sid, description="첫번째", amount=-1000
    )
    await _upload_simple_statement(
        client, headers, settlement_id=sid, description="두번째", amount=-2000
    )

    txs_before = await client.get(
        f"/api/v1/settlements/{sid}/bank-transactions", headers=headers
    )
    assert len(txs_before.json()) == 2

    del_resp = await client.delete(
        f"/api/v1/bank-statements/{first['id']}",
        headers=headers,
    )
    assert del_resp.status_code == 204, del_resp.text

    uploads = await client.get(
        f"/api/v1/settlements/{sid}/bank-statements", headers=headers
    )
    assert len(uploads.json()) == 1

    txs_after = await client.get(
        f"/api/v1/settlements/{sid}/bank-transactions", headers=headers
    )
    rows = txs_after.json()
    assert len(rows) == 1
    assert rows[0]["description"] == "두번째"


async def test_delete_upload_requires_treasurer_or_admin(client: AsyncClient) -> None:
    await signup(client, email="bs_del_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "bs_del_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])
    upload = await _upload_simple_statement(
        client, owner_headers, settlement_id=settlement["id"], description="x", amount=-1
    )

    await signup(client, email="bs_del_out@konkuk.ac.kr")
    out_headers = await auth_headers(client, "bs_del_out@konkuk.ac.kr")

    resp = await client.delete(
        f"/api/v1/bank-statements/{upload['id']}",
        headers=out_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_delete_upload_not_found(client: AsyncClient) -> None:
    await signup(client, email="bs_del_nf@konkuk.ac.kr")
    headers = await auth_headers(client, "bs_del_nf@konkuk.ac.kr")

    resp = await client.delete(
        f"/api/v1/bank-statements/{uuid.uuid4()}",
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


# --- Unit tests: date coercion + legacy .xls routing ----------------------


def test_coerce_date_accepts_kb_datetime_format() -> None:
    # KB 국민은행 '거래일시' is a dotted date + time string.
    assert _coerce_date("2026.06.07 16:59:05") == date(2026, 6, 7)
    assert _coerce_date("2026/06/07 16:59") == date(2026, 6, 7)
    assert _coerce_date("2026.06.07") == date(2026, 6, 7)
    assert _coerce_date("2026-06-07 16:59:05") == date(2026, 6, 7)


def test_parse_kakao_bank_export_sample() -> None:
    """Kakao Bank email xlsx uses strict OOXML + 구분/거래금액 columns."""
    file_bytes = _build_strict_ooxml_xlsx(
        [
            ["거래일시", "구분", "거래금액", "잔액", "거래명", "메모"],
            ["2026-06-08T19:30:14", "출금", 15000, 985000, "카카오페이", ""],
            ["2026-06-08T20:10:02", "출금", 50000, 935000, "넥슨캐시", ""],
            ["2026-06-09T09:00:00", "입금", 300000, 1235000, "김철수", "모임회비"],
            ["2026-06-09T12:15:44", "출금", 8500, 1226500, "스타벅스", ""],
            ["2026-06-10T08:00:00", "입금", 1000000, 2226500, "(주)회사명", "급여"],
        ]
    )
    transactions = parse_bank_statement_bytes(file_bytes)

    assert len(transactions) == 5

    by_description = {tx.description: tx for tx in transactions}
    assert by_description["카카오페이"].transaction_date == date(2026, 6, 8)
    assert by_description["카카오페이"].amount == Decimal("-15000")
    assert by_description["넥슨캐시"].amount == Decimal("-50000")
    assert by_description["김철수 (모임회비)"].amount == Decimal("300000")
    assert by_description["스타벅스"].amount == Decimal("-8500")
    assert by_description["(주)회사명 (급여)"].amount == Decimal("1000000")


def test_legacy_xls_routes_to_xlrd_reader() -> None:
    # An OLE2 header (binary .xls magic) that isn't a real workbook must be
    # handled by the xls reader (xlrd) — proving we don't hand .xls to openpyxl
    # (which would raise a different, misleading "not a zip" error).
    ole2_garbage = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128
    with pytest.raises(BankStatementParseError):
        parse_bank_statement_bytes(ole2_garbage)
