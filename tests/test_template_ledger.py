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
    assert mapping["A3"] == "title"
    assert mapping["G1"] == "generated_at"
    assert "columns" in mapping["_ledger"]
    assert "B3" not in mapping


def test_parse_month_sections_from_sample() -> None:
    from union_ledger.services.bank_statement import read_excel_sheet_rows

    rows = read_excel_sheet_rows(FIXTURE.read_bytes())
    sections = _parse_month_sections(rows)

    assert sections
    assert sections[0]["month"] == 12
    assert sections[0]["header_row"] < sections[0]["settlement_row"]


def test_render_audit_ledger_preserves_layout_and_fills_december() -> None:
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
        merchant_name="학생회\n비품구입",
        amount=Decimal("85000"),
        budget_category="비품",
        is_refund=False,
    )

    output = render_settlement_excel(
        template_path=FIXTURE,
        mapping_schema=mapping,
        spec_fields={
            "title": "컴퓨터공학부 학생회 2학기 결산안",
            "generated_at": "2026-06-10T12:00:00+00:00",
        },
        evidences=[evidence],
    )

    wb = load_workbook(io.BytesIO(output))
    sheet = wb.active
    assert sheet.title == "결산안"
    assert sheet["A3"].value == "컴퓨터공학부 학생회 2학기 결산안"
    assert "2026년" in str(sheet["G1"].value)
    assert sheet["A5"].value == "12월 결산"
    assert sheet["A6"].value == "날짜"

    found = False
    for row in range(7, 20):
        if sheet.cell(row=row, column=2).value == "학생회\n비품구입":
            assert sheet.cell(row=row, column=3).value == "비품"
            assert sheet.cell(row=row, column=5).value == 85000.0
            assert sheet.cell(row=row, column=8).value == "12*1"
            found = True
            break
    assert found, "expected December evidence row in audit ledger block"
    wb.close()
