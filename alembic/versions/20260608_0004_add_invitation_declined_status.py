"""Add 'declined' to the invitation_status enum.

Invited users can now reject an invitation they received
(``POST /invitations/{id}/decline``), which sets the status to ``declined``.
Postgres native enums need the new label added with ALTER TYPE before any
row can store it.

Revision ID: 20260608_0004
Revises: 20260607_0008
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260608_0004"
down_revision: str | Sequence[str] | None = "20260607_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS keeps the migration idempotent across re-runs. ADD VALUE
    # can't run inside a transaction block on older PG, so commit first.
    op.execute("COMMIT")
    op.execute(
        "ALTER TYPE invitation_status ADD VALUE IF NOT EXISTS 'declined'"
    )


def downgrade() -> None:
    # Postgres can't drop a single enum value without recreating the type.
    # Leaving the label in place is harmless; downgrade is a no-op.
    pass
