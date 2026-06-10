"""Add evidences.group_name (결산 장부의 구분 — 행사/용도 그룹).

The audit-ledger Excel groups rows by 구분 (e.g. "중간고사 간식행사") with an
항목 per line (the merchant). 구분 is context only the treasurer knows — it
cannot be extracted from a receipt — so it is a human-entered field applied
per upload batch. Nullable; existing rows simply have no group yet.

Revision ID: 20260610_0006
Revises: 20260608_0005
Create Date: 2026-06-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260610_0006"
down_revision: str | Sequence[str] | None = "20260608_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidences",
        sa.Column("group_name", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evidences", "group_name")
