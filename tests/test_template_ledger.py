"""Audit ledger template detection and rendering."""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from union_ledger.models.entities import Evidence
from union_ledger.models.enums import EvidenceStatus, EvidenceType
from union_ledger.services.artifact import render_settlement_excel
from union_ledger.services.template_ledger import (
    LAYOUT_AUDIT_LEDGER,
    _parse_month_sections,
    detect_audit_ledger_mapping,
)
from union_ledger.services.template_mapping import detect_mapping_schema_from_bytes

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "settlement_templates"
    / "audit_ledger_sample.xlsx"
)


def test_detect_audit_ledger_mapping_from_sample() -> None:
    file_bytes = FIXTURE.read_bytes()
    mapping = detect_mapping_schema_from_bytes(file_bytes)

    assert mapping["_layout"] == LAYOUT_AUDIT_LEDGER
    assert mapping["A1"] == "title"
    assert mapping["G1"] == "generated_at"
    assert mapping["A3"] == "title"
    assert "columns" in mapping["_ledger"]


def test_parse_month_sections_from_sample() -> None:
    from union_ledger.services.bank_statement import read_excel_sheet_rows

    rows = read_excel_sheet_rows(FIXTURE.read_bytes())
    sections = _parse_month_sections(rows)

    assert sections
    assert sections[0]["month"] == 12
    assert sections[0]["header_row"] < sections[0]["settlement_row"]


def test_render_audit_ledger_writes_header_and_december_rows() -> None:
    file_bytes = FIXTURE.read_bytes()
    mapping = detect_audit_ledger_mapping(file_bytes)
    assert mapping is not None

    evidence = Evidence(
        settlement_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        evidence_type=EvidenceType.PHYSICAL_RECEIPT,
        status=EvidenceStatus.CONFIRMED,
        source_file_name="receipt.png",
        source_file_path="/tmp/receipt.png",
        extracted_payload={},
        evidence_date=date(2025, 12, 23),
        merchant_name="스타벅스",
        amount=Decimal("8500"),
        budget_category="비품",
        is_refund=False,
    )

    output = render_settlement_excel(
        template_path=FIXTURE,
        mapping_schema=mapping,
        spec_fields={
            "title": "2025-2학기 결산안",
            "generated_at": "2026-06-10T12:00:00+00:00",
        },
        evidences=[evidence],
    )

    wb = load_workbook(io.BytesIO(output))
    sheet = wb.active
    assert sheet["A1"].value == "2025-2학기 결산안"
    assert sheet["A3"].value == "2025-2학기 결산안"
    assert "2026년" in str(sheet["G1"].value)

    # December block should contain the seeded evidence row.
    found = False
    for row in range(7, 20):
        if sheet[f"B{row}"].value == "스타벅스":
            assert sheet[f"E{row}"].value == 8500.0
            assert sheet[f"H{row}"].value == "12*1"
            found = True
            break
    assert found, "expected evidence row in December ledger block"
    wb.close()
