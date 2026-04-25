"""Settlement template services.

Spec ④ Step 1. The treasurer uploads an Excel file once per
academic-year/semester pair, then maps cells to spec fields. We persist the
file locally and store a `mapping_schema` JSON blob alongside it.

For this slice we accept a raw `mapping_schema` JSON in the request — the
auto-detection of Excel cell positions (which the spec mentions) is deferred
to a later PR. Treasurers can upload now and refine the mapping via PATCH.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.config import Settings
from union_ledger.models.entities import SettlementTemplate

SUPPORTED_TEMPLATE_SUFFIXES = {".xlsx", ".xls"}


class TemplateError(Exception):
    """Base for template-layer errors (maps to HTTP 4xx)."""


class TemplateNotFound(TemplateError):
    pass


class UnsupportedTemplateFormat(TemplateError):
    pass


class EmptyTemplateFile(TemplateError):
    pass


@dataclass(slots=True)
class StoredTemplate:
    original_name: str
    absolute_path: Path
    relative_path: Path
    size: int


def _validate_template_filename(filename: str) -> str:
    sanitized = Path(filename).name or "template.xlsx"
    suffix = Path(sanitized).suffix.lower()
    if suffix not in SUPPORTED_TEMPLATE_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_TEMPLATE_SUFFIXES))
        raise UnsupportedTemplateFormat(
            f"지원하지 않는 템플릿 파일 형식입니다. 지원 확장자: {supported}"
        )
    return sanitized


async def save_template_file(
    settings: Settings,
    *,
    organization_id: uuid.UUID,
    upload_file: UploadFile,
) -> StoredTemplate:
    original_name = _validate_template_filename(upload_file.filename or "template.xlsx")
    suffix = Path(original_name).suffix.lower()
    file_bytes = await upload_file.read()
    if not file_bytes:
        raise EmptyTemplateFile("비어 있는 파일은 업로드할 수 없습니다.")

    relative_path = (
        Path("settlement_templates") / str(organization_id) / f"{uuid.uuid4()}{suffix}"
    )
    absolute_path = (settings.storage_root / relative_path).resolve()
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(file_bytes)
    return StoredTemplate(
        original_name=original_name,
        absolute_path=absolute_path,
        relative_path=relative_path,
        size=len(file_bytes),
    )


async def create_template(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    stored_file: StoredTemplate,
    mapping_schema: dict[str, Any] | None = None,
) -> SettlementTemplate:
    template = SettlementTemplate(
        organization_id=organization_id,
        name=name,
        original_filename=stored_file.original_name,
        file_path=str(stored_file.absolute_path),
        mapping_schema=mapping_schema or {},
        is_active=True,
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def list_org_templates(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    include_inactive: bool = False,
) -> Sequence[SettlementTemplate]:
    stmt = (
        select(SettlementTemplate)
        .where(SettlementTemplate.organization_id == organization_id)
        .order_by(SettlementTemplate.created_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(SettlementTemplate.is_active.is_(True))
    result = await session.scalars(stmt)
    return list(result.all())


async def get_template_or_raise(
    session: AsyncSession,
    template_id: uuid.UUID,
) -> SettlementTemplate:
    template = await session.get(SettlementTemplate, template_id)
    if template is None:
        raise TemplateNotFound("템플릿을 찾을 수 없습니다.")
    return template


async def update_template(
    session: AsyncSession,
    *,
    template: SettlementTemplate,
    updates: dict[str, Any],
) -> SettlementTemplate:
    for field, value in updates.items():
        setattr(template, field, value)
    await session.commit()
    await session.refresh(template)
    return template
