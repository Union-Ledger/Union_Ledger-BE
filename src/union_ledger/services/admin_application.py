"""회장(admin) application services.

A school-verified user submits proof documents that they lead a student
organization. Platform operators (see ``require_operator``) review the
documents and approve or reject. On approval we create the organization and
make the applicant its ADMIN in a single transaction — this is the only
sanctioned path to an org-ADMIN role.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.models.entities import (
    Organization,
    OrganizationAdminApplication,
    OrganizationMembership,
)
from union_ledger.models.enums import AdminApplicationStatus, RoleType
from union_ledger.services.file_storage import StoredFile


class AdminApplicationError(Exception):
    """Base class for 회장-application errors (maps to HTTP 4xx)."""


class AdminApplicationNotFound(AdminApplicationError):
    pass


class AdminApplicationNotPending(AdminApplicationError):
    """The application has already been reviewed (approved/rejected)."""


def _documents_payload(stored_files: Sequence[StoredFile]) -> list[dict]:
    return [
        {
            "file_name": stored.original_name,
            "file_path": str(stored.absolute_path),
            "content_type": stored.content_type,
            "size": stored.size,
        }
        for stored in stored_files
    ]


async def create_application(
    session: AsyncSession,
    *,
    applicant_id: uuid.UUID,
    organization_name: str,
    college_name: str,
    department_name: str,
    stored_files: Sequence[StoredFile],
) -> OrganizationAdminApplication:
    application = OrganizationAdminApplication(
        applicant_user_id=applicant_id,
        organization_name=organization_name,
        college_name=college_name,
        department_name=department_name,
        documents=_documents_payload(stored_files),
        status=AdminApplicationStatus.PENDING,
    )
    session.add(application)
    await session.commit()
    await session.refresh(application)
    return application


async def list_applications(
    session: AsyncSession,
    *,
    status: AdminApplicationStatus | None = None,
) -> list[OrganizationAdminApplication]:
    stmt = select(OrganizationAdminApplication).order_by(
        OrganizationAdminApplication.created_at.desc()
    )
    if status is not None:
        stmt = stmt.where(OrganizationAdminApplication.status == status)
    result = await session.scalars(stmt)
    return list(result.all())


async def list_applications_for_user(
    session: AsyncSession,
    *,
    applicant_id: uuid.UUID,
) -> list[OrganizationAdminApplication]:
    result = await session.scalars(
        select(OrganizationAdminApplication)
        .where(OrganizationAdminApplication.applicant_user_id == applicant_id)
        .order_by(OrganizationAdminApplication.created_at.desc())
    )
    return list(result.all())


async def get_application_or_raise(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> OrganizationAdminApplication:
    application = await session.get(OrganizationAdminApplication, application_id)
    if application is None:
        raise AdminApplicationNotFound("신청 내역을 찾을 수 없습니다.")
    return application


async def approve_application(
    session: AsyncSession,
    *,
    application: OrganizationAdminApplication,
    reviewer_id: uuid.UUID,
    note: str | None = None,
) -> tuple[OrganizationAdminApplication, Organization]:
    """Approve: create the org + grant the applicant ADMIN, atomically.

    We build the org and membership with a flush (no intermediate commit) so
    the whole approval is one transaction and we never touch expired ORM state.
    """
    if application.status != AdminApplicationStatus.PENDING:
        raise AdminApplicationNotPending("이미 처리된 신청입니다.")

    organization = Organization(
        name=application.organization_name,
        college_name=application.college_name,
        department_name=application.department_name,
        created_by_id=application.applicant_user_id,
    )
    session.add(organization)
    await session.flush()

    session.add(
        OrganizationMembership(
            organization_id=organization.id,
            user_id=application.applicant_user_id,
            role=RoleType.ADMIN,
            is_primary=True,
        )
    )

    application.status = AdminApplicationStatus.APPROVED
    application.review_note = note
    application.reviewed_by_user_id = reviewer_id
    application.reviewed_at = datetime.now(UTC)
    application.created_organization_id = organization.id

    await session.commit()
    await session.refresh(application)
    await session.refresh(organization)
    return application, organization


async def reject_application(
    session: AsyncSession,
    *,
    application: OrganizationAdminApplication,
    reviewer_id: uuid.UUID,
    note: str | None = None,
) -> OrganizationAdminApplication:
    if application.status != AdminApplicationStatus.PENDING:
        raise AdminApplicationNotPending("이미 처리된 신청입니다.")
    application.status = AdminApplicationStatus.REJECTED
    application.review_note = note
    application.reviewed_by_user_id = reviewer_id
    application.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(application)
    return application
