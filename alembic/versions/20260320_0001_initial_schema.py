"""Initial schema for Union Ledger MVP."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260320_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


role_type = postgresql.ENUM("student", "treasurer", "auditor", "admin", name="role_type")
invited_role_type = postgresql.ENUM(
    "student",
    "treasurer",
    "auditor",
    "admin",
    name="invited_role_type",
)
invitation_type = postgresql.ENUM(
    "treasurer_invite",
    "auditor_invite",
    "role_transfer",
    name="invitation_type",
)
invitation_status = postgresql.ENUM(
    "pending",
    "accepted",
    "expired",
    "revoked",
    name="invitation_status",
)
settlement_status = postgresql.ENUM(
    "draft",
    "ready_for_review",
    "submitted",
    "under_audit",
    "approved",
    "rejected",
    "resubmitted",
    name="settlement_status",
)
evidence_type = postgresql.ENUM(
    "physical_receipt",
    "bank_transfer_statement",
    "e_receipt",
    name="evidence_type",
)
evidence_status = postgresql.ENUM(
    "uploaded",
    "extracting",
    "needs_review",
    "confirmed",
    "failed",
    name="evidence_status",
)
extraction_method = postgresql.ENUM("ocr", "pdf_text", name="extraction_method")
payment_method = postgresql.ENUM(
    "card",
    "bank_transfer",
    "online_payment",
    "other",
    name="payment_method",
)
bank_statement_status = postgresql.ENUM(
    "uploaded",
    "processing",
    "completed",
    "failed",
    name="bank_statement_status",
)
match_status = postgresql.ENUM(
    "matched",
    "amount_mismatch",
    "date_mismatch",
    "missing_bank_transaction",
    "missing_evidence",
    "manually_resolved",
    name="match_status",
)
artifact_type = postgresql.ENUM(
    "settlement_excel",
    "evidence_pdf",
    name="artifact_type",
)
artifact_status = postgresql.ENUM(
    "queued",
    "processing",
    "completed",
    "failed",
    name="artifact_status",
)
notification_type = postgresql.ENUM(
    "settlement_submitted",
    "audit_approved",
    "audit_rejected",
    "settlement_resubmitted",
    "settlement_published",
    name="notification_type",
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (
        role_type,
        invited_role_type,
        invitation_type,
        invitation_status,
        settlement_status,
        evidence_type,
        evidence_status,
        extraction_method,
        payment_method,
        bank_statement_status,
        match_status,
        artifact_type,
        artifact_status,
        notification_type,
    ):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column(
            "university_email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "organizations",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("college_name", sa.String(length=120), nullable=False),
        sa.Column("department_name", sa.String(length=120), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "organization_memberships",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", role_type, nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id", "role", name="uq_org_membership_role"),
    )

    op.create_table(
        "invitations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("invited_email", sa.String(length=255), nullable=False),
        sa.Column("invitation_type", invitation_type, nullable=False),
        sa.Column("role", invited_role_type, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("status", invitation_status, nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "settlement_templates",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("mapping_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "settlements",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("academic_year", sa.Integer(), nullable=False),
        sa.Column("semester", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", settlement_status, nullable=False, server_default="draft"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["settlement_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "evidences",
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", evidence_type, nullable=False),
        sa.Column("status", evidence_status, nullable=False, server_default="uploaded"),
        sa.Column("extraction_method", extraction_method, nullable=True),
        sa.Column("source_file_name", sa.String(length=255), nullable=False),
        sa.Column("source_file_path", sa.String(length=512), nullable=False),
        sa.Column("extracted_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_date", sa.Date(), nullable=True),
        sa.Column("merchant_name", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("payment_method", payment_method, nullable=True),
        sa.Column("budget_category", sa.String(length=120), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "bank_statement_uploads",
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("source_file_name", sa.String(length=255), nullable=False),
        sa.Column("source_file_path", sa.String(length=512), nullable=False),
        sa.Column("status", bank_statement_status, nullable=False, server_default="uploaded"),
        sa.Column("parsed_rows_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "bank_transactions",
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["upload_id"], ["bank_statement_uploads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "reconciliation_results",
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=True),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("status", match_status, nullable=False, server_default="matched"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bank_transaction_id"], ["bank_transactions.id"]),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidences.id"]),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_comments",
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=True),
        sa.Column("author_membership_id", sa.Uuid(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_membership_id"], ["organization_memberships.id"]),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidences.id"]),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "settlement_artifacts",
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", artifact_type, nullable=False),
        sa.Column("status", artifact_status, nullable=False, server_default="queued"),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("notification_type", notification_type, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("settlement_artifacts")
    op.drop_table("audit_comments")
    op.drop_table("reconciliation_results")
    op.drop_table("bank_transactions")
    op.drop_table("bank_statement_uploads")
    op.drop_table("evidences")
    op.drop_table("settlements")
    op.drop_table("settlement_templates")
    op.drop_table("invitations")
    op.drop_table("organization_memberships")
    op.drop_table("organizations")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    for enum in (
        notification_type,
        artifact_status,
        artifact_type,
        match_status,
        bank_statement_status,
        payment_method,
        extraction_method,
        evidence_status,
        evidence_type,
        settlement_status,
        invitation_status,
        invitation_type,
        invited_role_type,
        role_type,
    ):
        enum.drop(bind, checkfirst=True)
