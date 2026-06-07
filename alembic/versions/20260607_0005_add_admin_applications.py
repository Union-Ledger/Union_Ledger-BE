"""Add organization_admin_applications (회장 application + operator review).

A school-verified user submits proof documents to become a 회장(org ADMIN);
platform operators review and approve/reject. On approval the applicant becomes
ADMIN of a newly created organization.

Revision ID: 20260607_0005
Revises: 20260607_0004
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260607_0005"
down_revision: str | Sequence[str] | None = "20260607_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

admin_application_status = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    name="admin_application_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    admin_application_status.create(bind, checkfirst=True)
    op.create_table(
        "organization_admin_applications",
        sa.Column("applicant_user_id", sa.Uuid(), nullable=False),
        sa.Column("organization_name", sa.String(length=120), nullable=False),
        sa.Column("college_name", sa.String(length=120), nullable=False),
        sa.Column("department_name", sa.String(length=120), nullable=False),
        sa.Column(
            "documents",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            admin_application_status,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_organization_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["applicant_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("organization_admin_applications")
    admin_application_status.drop(op.get_bind(), checkfirst=True)
