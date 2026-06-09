"""Add evidences.is_refund (refund/cancellation receipts net out in rollups).

Refund receipts (환불·취소·반품) are still evidence but should SUBTRACT from the
settlement totals rather than add. We flag them with is_refund; the per-category
and total aggregations apply a negative sign, while the amount column stays a
positive magnitude. Existing rows default to false (not refunds).

Revision ID: 20260608_0005
Revises: 20260608_0004
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260608_0005"
down_revision: str | Sequence[str] | None = "20260608_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidences",
        sa.Column(
            "is_refund",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("evidences", "is_refund")
