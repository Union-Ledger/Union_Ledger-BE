"""Bank statement schemas (spec §2 Step 3).

The treasurer uploads a bank-issued .xlsx of transactions for a settlement
period. The service parses rows into BankTransaction records, then the
reconciliation service matches them against Evidence by date+amount.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from union_ledger.models.enums import BankStatementStatus


class BankTransactionResponse(BaseModel):
    id: uuid.UUID
    upload_id: uuid.UUID
    transaction_date: date
    description: str
    amount: Decimal
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BankStatementUploadResponse(BaseModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    source_file_name: str
    source_file_path: str
    status: BankStatementStatus
    parsed_rows_count: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
