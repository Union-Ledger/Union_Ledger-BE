"""Rename the org role enum value 'admin' -> 'president' (회장).

The organization-admin role is renamed to 'president' (회장) for clarity — it
was easy to confuse with platform operators. RoleType is materialized as two
Postgres enum types (``role_type`` on memberships, ``invited_role_type`` on
invitations), so the value is renamed in both. ``ALTER TYPE ... RENAME VALUE``
relabels the enum in place, so existing rows are preserved automatically.

Revision ID: 20260607_0007
Revises: 20260607_0006
Create Date: 2026-06-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260607_0007"
down_revision: str | Sequence[str] | None = "20260607_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/others store the enum as a plain string — nothing to rename.
        return
    op.execute("ALTER TYPE role_type RENAME VALUE 'admin' TO 'president'")
    op.execute("ALTER TYPE invited_role_type RENAME VALUE 'admin' TO 'president'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TYPE role_type RENAME VALUE 'president' TO 'admin'")
    op.execute("ALTER TYPE invited_role_type RENAME VALUE 'president' TO 'admin'")
