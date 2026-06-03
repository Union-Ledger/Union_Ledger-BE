"""Add settlements.view_count for student public viewer analytics.

Revision ID: 20260603_0003
Revises: 20260422_0002
Create Date: 2026-06-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260603_0003"
down_revision: str | Sequence[str] | None = "20260422_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settlements",
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("settlements", "view_count")
