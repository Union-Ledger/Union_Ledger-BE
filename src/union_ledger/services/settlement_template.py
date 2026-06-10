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

from union_ledger.services.file_storage import read_upload_within_limit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from union_ledger.core.config import Settings
from union_ledger.models.entities import Settlement, SettlementTemplate
from union_ledger.services.template_mapping import detect_mapping_schema_from_bytes
from union_ledger.services.template_ledger import LAYOUT_AUDIT_LEDGER

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
    file_bytes = await read_upload_within_limit(
        upload_file, settings.max_upload_size_mb * 1024 * 1024
    )
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


def resolve_mapping_schema(
    file_bytes: bytes,
    mapping_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Prefer audit-ledger auto-detection; ignore stale summary mappings from the FE."""
    detected = detect_mapping_schema_from_bytes(file_bytes)
    explicit = mapping_schema or {}
    if detected.get("_layout") == LAYOUT_AUDIT_LEDGER:
        return detected
    if explicit:
        return explicit
    return detected


async def effective_mapping_schema(
    session: AsyncSession,
    *,
    template: SettlementTemplate,
) -> dict[str, Any]:
    """Return mapping for generation, always re-read from the template file."""
    path = Path(template.file_path)
    if not path.is_file():
        return template.mapping_schema or {}

    detected = detect_mapping_schema_from_bytes(path.read_bytes())
    if not detected:
        return template.mapping_schema or {}

    # Detection augments — it must never discard cells the treasurer mapped
    # explicitly. Manual entries win on collision; detected supplies the rest
    # (incl. _layout/_ledger metadata, which manual mappings never contain).
    merged = {**detected, **(template.mapping_schema or {})}

    if template.mapping_schema != merged:
        template.mapping_schema = merged
        await session.commit()
        await session.refresh(template)
    return merged


async def get_active_org_template(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
) -> SettlementTemplate | None:
    return await session.scalar(
        select(SettlementTemplate)
        .where(
            SettlementTemplate.organization_id == organization_id,
            SettlementTemplate.is_active.is_(True),
        )
        .order_by(SettlementTemplate.created_at.desc())
        .limit(1)
    )


async def resolve_template_for_generation(
    session: AsyncSession,
    *,
    settlement: Settlement,
) -> SettlementTemplate:
    """Use the org's current active template — not the draft-time snapshot.

    Treasurers often upload the correct audit workbook after the settlement
    draft was already created. Pinning ``settlement.template_id`` at creation
    left Excel generation cloning an old summary template forever.
    """
    active = await get_active_org_template(
        session, organization_id=settlement.organization_id
    )
    if active is not None:
        if settlement.template_id != active.id:
            settlement.template_id = active.id
            await session.commit()
            await session.refresh(settlement)
        return active

    if settlement.template_id is None:
        raise TemplateNotFound("등록된 결산 템플릿이 없습니다.")

    template = await session.get(SettlementTemplate, settlement.template_id)
    if template is None:
        raise TemplateNotFound("템플릿이 삭제되었습니다.")
    return template


async def deactivate_org_templates(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    except_template_id: uuid.UUID | None = None,
) -> None:
    templates = (
        await session.scalars(
            select(SettlementTemplate).where(
                SettlementTemplate.organization_id == organization_id,
                SettlementTemplate.is_active.is_(True),
            )
        )
    ).all()
    changed = False
    for template in templates:
        if except_template_id is not None and template.id == except_template_id:
            continue
        template.is_active = False
        changed = True
    if changed:
        await session.commit()


async def create_template(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    stored_file: StoredTemplate,
    mapping_schema: dict[str, Any] | None = None,
    file_bytes: bytes | None = None,
) -> SettlementTemplate:
    resolved_mapping = (
        resolve_mapping_schema(file_bytes, mapping_schema)
        if file_bytes
        else (mapping_schema or {})
    )

    template = SettlementTemplate(
        organization_id=organization_id,
        name=name,
        original_filename=stored_file.original_name,
        file_path=str(stored_file.absolute_path),
        mapping_schema=resolved_mapping,
        is_active=True,
    )
    session.add(template)
    await session.flush()
    await deactivate_org_templates(
        session,
        organization_id=organization_id,
        except_template_id=template.id,
    )
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
