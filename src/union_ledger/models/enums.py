from enum import StrEnum


class RoleType(StrEnum):
    STUDENT = "student"
    TREASURER = "treasurer"
    AUDITOR = "auditor"
    ADMIN = "admin"


class InvitationType(StrEnum):
    TREASURER_INVITE = "treasurer_invite"
    AUDITOR_INVITE = "auditor_invite"
    ROLE_TRANSFER = "role_transfer"


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SettlementStatus(StrEnum):
    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
    SUBMITTED = "submitted"
    UNDER_AUDIT = "under_audit"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESUBMITTED = "resubmitted"


class EvidenceType(StrEnum):
    PHYSICAL_RECEIPT = "physical_receipt"
    BANK_TRANSFER_STATEMENT = "bank_transfer_statement"
    E_RECEIPT = "e_receipt"


class EvidenceStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ExtractionMethod(StrEnum):
    OCR = "ocr"
    PDF_TEXT = "pdf_text"


class PaymentMethod(StrEnum):
    CARD = "card"
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    ONLINE_PAYMENT = "online_payment"
    OTHER = "other"


class BankStatementStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class MatchStatus(StrEnum):
    MATCHED = "matched"
    AMOUNT_MISMATCH = "amount_mismatch"
    DATE_MISMATCH = "date_mismatch"
    MISSING_BANK_TRANSACTION = "missing_bank_transaction"
    MISSING_EVIDENCE = "missing_evidence"
    MANUALLY_RESOLVED = "manually_resolved"


class ArtifactType(StrEnum):
    SETTLEMENT_EXCEL = "settlement_excel"
    EVIDENCE_PDF = "evidence_pdf"


class ArtifactStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class NotificationType(StrEnum):
    SETTLEMENT_SUBMITTED = "settlement_submitted"
    AUDIT_APPROVED = "audit_approved"
    AUDIT_REJECTED = "audit_rejected"
    SETTLEMENT_RESUBMITTED = "settlement_resubmitted"
    SETTLEMENT_PUBLISHED = "settlement_published"
