"""Add users.password_hash.

Teammate's PR #2 added `User.password_hash` to the ORM model but the matching
migration was missing, so `alembic upgrade head` left Postgres without the
column. Without this, signup explodes at INSERT time with UndefinedColumn.

Revision ID: 20260422_0002
Revises: 20260320_0001
Create Date: 2026-04-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260422_0002"
down_revision: str | Sequence[str] | None = "20260320_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable to match the ORM model (`Mapped[str | None]`). Any pre-existing
    # rows (there shouldn't be any — auth hadn't shipped) get NULL, which
    # login.py treats as "no credentials" and rejects.
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_hash")
