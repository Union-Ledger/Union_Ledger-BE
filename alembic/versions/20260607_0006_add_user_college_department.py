"""Add users.college_name / users.department_name (student school identity).

Self-signup no longer creates an organization, so a student's college/department
(collected at signup) is stored on the user. The student viewer/dashboard
resolves the student's department council by matching these columns.

Revision ID: 20260607_0006
Revises: 20260607_0005
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260607_0006"
down_revision: str | Sequence[str] | None = "20260607_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("college_name", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("department_name", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "department_name")
    op.drop_column("users", "college_name")
