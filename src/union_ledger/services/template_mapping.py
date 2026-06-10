"""Settlement template cell-mapping auto-detection.

Treasurers upload a school-specific blank settlement xlsx. When no
`mapping_schema` is supplied we scan label cells (e.g. "제목", "총 지출액")
and map the adjacent value cell to a supported spec field so artifact
generation can fill the workbook without manual JSON editing.
"""

from __future__ import annotations

from typing import Any

from openpyxl.utils import get_column_letter

from union_ledger.services.bank_statement import read_excel_sheet_rows

# (keywords in label cell, spec field name). Order matters — more specific first.
_LABEL_FIELD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("결산안제목", "결산안명", "결산제목", "제목"), "title"),
    (("학년도", "학년", "년도"), "academic_year"),
    (("학기",), "semester"),
    (("단과대학", "단과대"), "college_name"),
    (("학과", "학부", "전공"), "department_name"),
    (("단체명", "학생회", "조직명"), "organization_name"),
    (("총지출액", "총지출", "지출합계", "지출총액"), "total_evidence_amount"),
    (("증빙건수", "증빙수", "증빙자료건수"), "evidence_count"),
    (("거래내역건수", "거래건수", "은행거래건수"), "bank_transaction_count"),
    (("매칭건수", "일치건수", "대조일치"), "matched_count"),
    (("금액불일치", "금액불일치건수"), "amount_mismatch_count"),
    (("날짜불일치", "날짜불일치건수"), "date_mismatch_count"),
    (("거래내역누락", "거래누락"), "missing_bank_transaction_count"),
    (("증빙누락",), "missing_evidence_count"),
    (("제출일", "제출일시"), "submitted_at"),
    (("감사일", "감사일시", "승인일"), "audited_at"),
    (("공개일", "공개일시"), "published_at"),
)


def _normalize_label(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace(" ", "").replace(":", "").lower()


def _match_field(label: str) -> str | None:
    if not label:
        return None
    for keywords, field_name in _LABEL_FIELD_RULES:
        if any(keyword in label for keyword in keywords):
            return field_name
    return None


def _cell_address(row_idx: int, col_idx: int) -> str:
    return f"{get_column_letter(col_idx + 1)}{row_idx + 1}"


def _value_cell_for_label(
    rows: list[list[object]],
    *,
    row_idx: int,
    col_idx: int,
) -> tuple[int, int]:
    """Return the value cell beside a label — usually one column to the right."""
    row = rows[row_idx]
    value_col = col_idx + 1
    if value_col < len(row):
        cell_val = row[value_col]
        if cell_val not in (None, "") and _match_field(_normalize_label(cell_val)) is None:
            return row_idx, value_col
    return row_idx, value_col


def detect_mapping_schema_from_bytes(file_bytes: bytes) -> dict[str, Any]:
    """Scan the first worksheet and return `{cell: spec_field}` mappings."""
    if not file_bytes:
        return {}

    try:
        rows = read_excel_sheet_rows(file_bytes)
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    used_fields: set[str] = set()

    max_scan_rows = min(len(rows), 80)
    for row_idx in range(max_scan_rows):
        row = rows[row_idx]
        for col_idx, raw in enumerate(row):
            field_name = _match_field(_normalize_label(raw))
            if field_name is None or field_name in used_fields:
                continue
            value_row, value_col = _value_cell_for_label(rows, row_idx=row_idx, col_idx=col_idx)
            mapping[_cell_address(value_row, value_col)] = field_name
            used_fields.add(field_name)

    return mapping
