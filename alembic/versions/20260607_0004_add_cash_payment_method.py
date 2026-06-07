"""Add 'cash' to the payment_method enum.

The ``PaymentMethod`` enum (``models/enums.py``) and the OCR extraction
pipeline both produce ``cash``, but the initial schema migration created the
Postgres ``payment_method`` enum with only card/bank_transfer/online_payment/
other. Saving or extracting a cash receipt therefore fails on Postgres with
``invalid input value for enum payment_method: "cash"``. The test suite runs
on SQLite (where enums are plain strings) so it never caught this. This
migration adds the missing value.

Revision ID: 20260607_0004
Revises: 20260603_0003
Create Date: 2026-06-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260607_0004"
down_revision: str | Sequence[str] | None = "20260603_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # On SQLite/others the column is stored as a plain string with no
        # enum type to alter — nothing to do.
        return
    # ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block, so
    # we step outside Alembic's per-migration transaction. ``IF NOT EXISTS``
    # keeps the migration idempotent if 'cash' was added manually.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'cash'")


def downgrade() -> None:
    # Postgres has no first-class way to drop a single enum value; reversing
    # this would require recreating the type and rewriting every dependent
    # column. 'cash' is a legitimate domain value, so we leave it in place.
    pass
