from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from union_ledger.models.base import TimestampedUUIDModel
from union_ledger.models.enums import (
    AdminApplicationStatus,
    ArtifactStatus,
    ArtifactType,
    BankStatementStatus,
    EvidenceStatus,
    EvidenceType,
    ExtractionMethod,
    InvitationStatus,
    InvitationType,
    MatchStatus,
    NotificationType,
    PaymentMethod,
    RoleType,
    SettlementStatus,
)

# Postgres-native JSONB for production; fall back to generic JSON on sqlite
# so the in-memory aiosqlite test engine can still compile DDL.
_JSON_COLUMN = JSONB().with_variant(JSON(), "sqlite")


def _enum_column(enum_cls: type, name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda enum_values: [member.value for member in enum_values],
    )


class User(TimestampedUUIDModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(120))
    # A student's school identity, captured at signup. Students are NOT org
    # members, so the student viewer/dashboard resolves "my department council"
    # by matching these against organizations' college/department.
    college_name: Mapped[str | None] = mapped_column(String(120))
    department_name: Mapped[str | None] = mapped_column(String(120))
    university_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    memberships: Mapped[list[OrganizationMembership]] = relationship(back_populates="user")
    notifications: Mapped[list[Notification]] = relationship(back_populates="user")


class Organization(TimestampedUUIDModel):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    college_name: Mapped[str] = mapped_column(String(120), nullable=False)
    department_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    memberships: Mapped[list[OrganizationMembership]] = relationship(back_populates="organization")
    templates: Mapped[list[SettlementTemplate]] = relationship(back_populates="organization")
    settlements: Mapped[list[Settlement]] = relationship(back_populates="organization")


class OrganizationMembership(TimestampedUUIDModel):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", "role", name="uq_org_membership_role"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[RoleType] = mapped_column(_enum_column(RoleType, "role_type"), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Invitation(TimestampedUUIDModel):
    __tablename__ = "invitations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
    )
    invited_email: Mapped[str] = mapped_column(String(255), nullable=False)
    invitation_type: Mapped[InvitationType] = mapped_column(
        _enum_column(InvitationType, "invitation_type"),
        nullable=False,
    )
    role: Mapped[RoleType] = mapped_column(
        _enum_column(RoleType, "invited_role_type"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[InvitationStatus] = mapped_column(
        _enum_column(InvitationStatus, "invitation_status"),
        default=InvitationStatus.PENDING,
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganizationAdminApplication(TimestampedUUIDModel):
    """회장(admin) application.

    A school-verified user submits proof documents that they lead a student
    organization; platform operators review and approve or reject. On approval
    the applicant becomes ADMIN of a newly created organization. This is the
    only sanctioned way to obtain an org-ADMIN role (self-signup no longer
    grants it).
    """

    __tablename__ = "organization_admin_applications"

    applicant_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
    organization_name: Mapped[str] = mapped_column(String(120), nullable=False)
    college_name: Mapped[str] = mapped_column(String(120), nullable=False)
    department_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Uploaded proof documents: [{file_name, file_path, content_type, size}, ...].
    documents: Mapped[list] = mapped_column(_JSON_COLUMN, default=list, nullable=False)
    status: Mapped[AdminApplicationStatus] = mapped_column(
        _enum_column(AdminApplicationStatus, "admin_application_status"),
        default=AdminApplicationStatus.PENDING,
        nullable=False,
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id")
    )


class SettlementTemplate(TimestampedUUIDModel):
    __tablename__ = "settlement_templates"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mapping_schema: Mapped[dict] = mapped_column(_JSON_COLUMN, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="templates")
    settlements: Mapped[list[Settlement]] = relationship(back_populates="template")


class Settlement(TimestampedUUIDModel):
    __tablename__ = "settlements"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("settlement_templates.id"))
    academic_year: Mapped[int] = mapped_column(nullable=False)
    semester: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SettlementStatus] = mapped_column(
        _enum_column(SettlementStatus, "settlement_status"),
        default=SettlementStatus.DRAFT,
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    view_count: Mapped[int] = mapped_column(default=0, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="settlements")
    template: Mapped[SettlementTemplate | None] = relationship(back_populates="settlements")
    evidences: Mapped[list[Evidence]] = relationship(back_populates="settlement")
    bank_statement_uploads: Mapped[list[BankStatementUpload]] = relationship(
        back_populates="settlement"
    )
    reconciliation_results: Mapped[list[ReconciliationResult]] = relationship(
        back_populates="settlement"
    )
    audit_comments: Mapped[list[AuditComment]] = relationship(back_populates="settlement")
    artifacts: Mapped[list[SettlementArtifact]] = relationship(back_populates="settlement")


class Evidence(TimestampedUUIDModel):
    __tablename__ = "evidences"

    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
    )
    evidence_type: Mapped[EvidenceType] = mapped_column(
        _enum_column(EvidenceType, "evidence_type"),
        nullable=False,
    )
    status: Mapped[EvidenceStatus] = mapped_column(
        _enum_column(EvidenceStatus, "evidence_status"),
        default=EvidenceStatus.UPLOADED,
        nullable=False,
    )
    extraction_method: Mapped[ExtractionMethod | None] = mapped_column(
        _enum_column(ExtractionMethod, "extraction_method")
    )
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    extracted_payload: Mapped[dict] = mapped_column(_JSON_COLUMN, default=dict, nullable=False)
    evidence_date: Mapped[date | None] = mapped_column(Date)
    merchant_name: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[PaymentMethod | None] = mapped_column(
        _enum_column(PaymentMethod, "payment_method")
    )
    budget_category: Mapped[str | None] = mapped_column(String(120))

    settlement: Mapped[Settlement] = relationship(back_populates="evidences")


class BankStatementUpload(TimestampedUUIDModel):
    __tablename__ = "bank_statement_uploads"

    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[BankStatementStatus] = mapped_column(
        _enum_column(BankStatementStatus, "bank_statement_status"),
        default=BankStatementStatus.UPLOADED,
        nullable=False,
    )
    parsed_rows_count: Mapped[int] = mapped_column(default=0, nullable=False)

    settlement: Mapped[Settlement] = relationship(back_populates="bank_statement_uploads")
    transactions: Mapped[list[BankTransaction]] = relationship(back_populates="upload")


class BankTransaction(TimestampedUUIDModel):
    __tablename__ = "bank_transactions"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bank_statement_uploads.id"),
        nullable=False,
    )
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    upload: Mapped[BankStatementUpload] = relationship(back_populates="transactions")


class ReconciliationResult(TimestampedUUIDModel):
    __tablename__ = "reconciliation_results"

    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidences.id"))
    bank_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_transactions.id")
    )
    status: Mapped[MatchStatus] = mapped_column(
        _enum_column(MatchStatus, "match_status"),
        default=MatchStatus.MATCHED,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    settlement: Mapped[Settlement] = relationship(back_populates="reconciliation_results")


class AuditComment(TimestampedUUIDModel):
    __tablename__ = "audit_comments"

    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidences.id"))
    author_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_memberships.id")
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False)

    settlement: Mapped[Settlement] = relationship(back_populates="audit_comments")


class SettlementArtifact(TimestampedUUIDModel):
    __tablename__ = "settlement_artifacts"

    settlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    artifact_type: Mapped[ArtifactType] = mapped_column(
        _enum_column(ArtifactType, "artifact_type"),
        nullable=False,
    )
    status: Mapped[ArtifactStatus] = mapped_column(
        _enum_column(ArtifactStatus, "artifact_status"),
        default=ArtifactStatus.QUEUED,
        nullable=False,
    )
    file_path: Mapped[str | None] = mapped_column(String(512))

    settlement: Mapped[Settlement] = relationship(back_populates="artifacts")


class Notification(TimestampedUUIDModel):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    notification_type: Mapped[NotificationType] = mapped_column(
        _enum_column(NotificationType, "notification_type"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="notifications")
