"""Bank statement upload + xlsx parser (spec §2 Step 3).

Korean banks export per-bank custom xlsx layouts (sometimes with a few rows
of metadata before the table header), so the parser walks the sheet to find
the header row by keyword matching, then reads `transaction_date`,
`description`, and `amount` columns.

Heuristic header keywords (case/whitespace insensitive):
  - date         : 거래일, 거래일자, 일자, 날짜, transaction_date, date
  - description  : 적요, 내용, 거래내역, description, memo
  - withdrawal   : 출금, 출금액, withdrawal
  - deposit      : 입금, 입금액, deposit
  - amount       : 금액, 거래금액, amount

Sign convention for `amount`:
  - If the sheet has a single signed `amount` column, we keep the sign.
  - If it has separate withdrawal/deposit columns, withdrawals become
    negative and deposits stay positive — settlements track expenses, so
    most matched rows will be negative.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.config import Settings
from union_ledger.models.entities import BankStatementUpload, BankTransaction
from union_ledger.models.enums import BankStatementStatus

SUPPORTED_BANK_STATEMENT_SUFFIXES = {".xlsx", ".xlsm"}

_DATE_KEYWORDS = ("거래일", "거래일자", "일자", "날짜", "transaction_date", "date")
_DESC_KEYWORDS = ("적요", "내용", "거래내역", "description", "memo", "비고")
_WITHDRAWAL_KEYWORDS = ("출금", "출금액", "withdrawal", "출금금액")
_DEPOSIT_KEYWORDS = ("입금", "입금액", "deposit", "입금금액")
_AMOUNT_KEYWORDS = ("금액", "거래금액", "amount")

# Maximum rows to scan when looking for the header row. Korean bank exports
# rarely push the header past row 20.
_MAX_HEADER_SCAN_ROWS = 30


class BankStatementError(Exception):
    """Base for bank-statement-layer errors (maps to HTTP 4xx)."""


class UnsupportedBankStatementFormat(BankStatementError):
    pass


class EmptyBankStatementFile(BankStatementError):
    pass


class BankStatementParseError(BankStatementError):
    pass


class BankStatementUploadNotFound(BankStatementError):
    pass


@dataclass(slots=True)
class StoredBankStatement:
    original_name: str
    absolute_path: Path
    relative_path: Path
    size: int


@dataclass(slots=True)
class ParsedTransaction:
    transaction_date: date
    description: str
    amount: Decimal


def _validate_bank_statement_filename(filename: str) -> str:
    sanitized = Path(filename).name or "bank_statement.xlsx"
    suffix = Path(sanitized).suffix.lower()
    if suffix not in SUPPORTED_BANK_STATEMENT_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_BANK_STATEMENT_SUFFIXES))
        raise UnsupportedBankStatementFormat(
            f"지원하지 않는 거래내역 파일 형식입니다. 지원 확장자: {supported}"
        )
    return sanitized


async def save_bank_statement_file(
    settings: Settings,
    *,
    settlement_id: uuid.UUID,
    upload_file: UploadFile,
) -> StoredBankStatement:
    original_name = _validate_bank_statement_filename(upload_file.filename or "bank_statement.xlsx")
    suffix = Path(original_name).suffix.lower()
    file_bytes = await upload_file.read()
    if not file_bytes:
        raise EmptyBankStatementFile("비어 있는 파일은 업로드할 수 없습니다.")

    relative_path = (
        Path("bank_statements") / str(settlement_id) / f"{uuid.uuid4()}{suffix}"
    )
    absolute_path = (settings.storage_root / relative_path).resolve()
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(file_bytes)
    return StoredBankStatement(
        original_name=original_name,
        absolute_path=absolute_path,
        relative_path=relative_path,
        size=len(file_bytes),
    )


def _normalize_header_cell(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).lower()


def _header_matches(cell: str, keywords: Iterable[str]) -> bool:
    return any(keyword.lower() in cell for keyword in keywords)


def _coerce_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Common Korean bank exports: "2026-04-29", "2026.04.29", "20260429"
    candidates = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y%m%d",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _coerce_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    # Strip currency symbols, commas, whitespace.
    text = text.replace(",", "").replace("₩", "").replace("원", "").strip()
    if text in {"-", ""}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _find_header_row(rows: Sequence[Sequence[object]]) -> tuple[int, dict[str, int]]:
    """Locate the header row + column index map.

    Returns (row_index, column_map) where column_map has keys:
      "date", "description", "amount" (if present),
      "withdrawal", "deposit" (if present)
    Raises BankStatementParseError if no recognizable header found.
    """
    for r_idx, row in enumerate(rows[:_MAX_HEADER_SCAN_ROWS]):
        column_map: dict[str, int] = {}
        for c_idx, raw in enumerate(row):
            cell = _normalize_header_cell(raw)
            if not cell:
                continue
            matched_withdrawal = _header_matches(cell, _WITHDRAWAL_KEYWORDS)
            matched_deposit = _header_matches(cell, _DEPOSIT_KEYWORDS)
            if "date" not in column_map and _header_matches(cell, _DATE_KEYWORDS):
                column_map["date"] = c_idx
            if "description" not in column_map and _header_matches(cell, _DESC_KEYWORDS):
                column_map["description"] = c_idx
            if "withdrawal" not in column_map and matched_withdrawal:
                column_map["withdrawal"] = c_idx
            if "deposit" not in column_map and matched_deposit:
                column_map["deposit"] = c_idx
            # "출금액"/"입금액" also contain "금액", so they'd match the generic
            # amount keyword too. Never classify a withdrawal/deposit column as
            # the signed `amount` column — otherwise a statement with separate
            # 출금액/입금액 columns reads only one of them and drops the other
            # (e.g. deposit-only 학생회비 rows → 0 parsed → empty reconciliation).
            if (
                "amount" not in column_map
                and not matched_withdrawal
                and not matched_deposit
                and _header_matches(cell, _AMOUNT_KEYWORDS)
            ):
                column_map["amount"] = c_idx
        has_amount_signal = (
            "amount" in column_map
            or "withdrawal" in column_map
            or "deposit" in column_map
        )
        if "date" in column_map and has_amount_signal:
            # Description is optional but common — fall back to empty string at parse time.
            return r_idx, column_map
    raise BankStatementParseError(
        "거래내역 엑셀에서 헤더(거래일/금액 등)를 찾을 수 없습니다."
    )


def parse_bank_statement_bytes(file_bytes: bytes) -> list[ParsedTransaction]:
    """Parse an xlsx into ParsedTransaction list. Pure function for testing."""
    try:
        wb = load_workbook(BytesIO(file_bytes), data_only=True, read_only=True)
    except (InvalidFileException, KeyError, OSError) as exc:
        raise BankStatementParseError(
            f"엑셀 파일을 열 수 없습니다: {exc}"
        ) from exc

    sheet = wb.active
    if sheet is None:
        raise BankStatementParseError("엑셀에 시트가 없습니다.")

    # Read everything up-front; bank statements are typically <10k rows.
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    wb.close()
    if not rows:
        raise BankStatementParseError("엑셀이 비어 있습니다.")

    header_idx, columns = _find_header_row(rows)
    transactions: list[ParsedTransaction] = []

    for row in rows[header_idx + 1 :]:
        if not row or all(cell is None or cell == "" for cell in row):
            continue
        d = _coerce_date(_safe_get(row, columns.get("date")))
        if d is None:
            continue

        description_raw = _safe_get(row, columns.get("description"))
        description = str(description_raw).strip() if description_raw is not None else ""

        amount = _resolve_amount(row, columns)
        if amount is None:
            continue

        transactions.append(
            ParsedTransaction(
                transaction_date=d,
                description=description[:255],
                amount=amount,
            )
        )

    return transactions


def _safe_get(row: Sequence[object], idx: int | None) -> object:
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _resolve_amount(row: Sequence[object], columns: dict[str, int]) -> Decimal | None:
    """Resolve a single signed Decimal from either:
      - a single `amount` column (used as-is), or
      - a withdrawal/deposit pair (withdrawal becomes negative).
    """
    if "amount" in columns:
        return _coerce_decimal(_safe_get(row, columns["amount"]))

    withdrawal = _coerce_decimal(_safe_get(row, columns.get("withdrawal")))
    deposit = _coerce_decimal(_safe_get(row, columns.get("deposit")))

    if withdrawal and withdrawal != Decimal(0):
        return -abs(withdrawal)
    if deposit and deposit != Decimal(0):
        return abs(deposit)
    return None


async def create_upload_with_transactions(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
    stored_file: StoredBankStatement,
    transactions: Sequence[ParsedTransaction],
) -> BankStatementUpload:
    upload = BankStatementUpload(
        settlement_id=settlement_id,
        source_file_name=stored_file.original_name,
        source_file_path=str(stored_file.absolute_path),
        status=(
            BankStatementStatus.COMPLETED if transactions else BankStatementStatus.FAILED
        ),
        parsed_rows_count=len(transactions),
    )
    session.add(upload)
    await session.flush()

    for tx in transactions:
        session.add(
            BankTransaction(
                upload_id=upload.id,
                transaction_date=tx.transaction_date,
                description=tx.description,
                amount=tx.amount,
            )
        )

    await session.commit()
    await session.refresh(upload)
    return upload


async def list_settlement_uploads(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
) -> Sequence[BankStatementUpload]:
    stmt = (
        select(BankStatementUpload)
        .where(BankStatementUpload.settlement_id == settlement_id)
        .order_by(BankStatementUpload.created_at.desc())
    )
    result = await session.scalars(stmt)
    return list(result.all())


async def list_settlement_transactions(
    session: AsyncSession,
    *,
    settlement_id: uuid.UUID,
) -> Sequence[BankTransaction]:
    """All bank transactions across every upload for the settlement."""
    stmt = (
        select(BankTransaction)
        .join(BankStatementUpload, BankTransaction.upload_id == BankStatementUpload.id)
        .where(BankStatementUpload.settlement_id == settlement_id)
        .order_by(BankTransaction.transaction_date.desc(), BankTransaction.created_at.desc())
    )
    result = await session.scalars(stmt)
    return list(result.all())
