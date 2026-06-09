from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from union_ledger.core.config import Settings

SUPPORTED_EVIDENCE_SUFFIXES = {
    ".bmp",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

_UPLOAD_READ_CHUNK = 1024 * 1024  # 1 MiB


class FileTooLargeError(Exception):
    """Raised when an upload exceeds the configured size cap.

    Deliberately NOT a subclass of ValueError so endpoint-local
    `except ValueError` handlers don't swallow it — a single global handler in
    main.py maps it to HTTP 413.
    """


async def read_upload_within_limit(upload_file: UploadFile, max_bytes: int) -> bytes:
    """Read an UploadFile fully but abort if it exceeds ``max_bytes``.

    Reads in chunks so a malicious/huge upload can't be buffered into memory
    before we notice — we stop as soon as the cap is crossed.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload_file.read(_UPLOAD_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            limit_mb = max_bytes // (1024 * 1024)
            raise FileTooLargeError(
                f"파일이 너무 큽니다. 최대 {limit_mb}MB 까지 업로드할 수 있습니다."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(slots=True)
class StoredFile:
    original_name: str
    absolute_path: Path
    relative_path: Path
    size: int
    content_type: str | None


class LocalFileStorage:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.storage_root
        self._max_upload_bytes = settings.max_upload_size_mb * 1024 * 1024

    async def save_evidence_file(
        self,
        settlement_id: uuid.UUID,
        upload_file: UploadFile,
    ) -> StoredFile:
        return await self._save_upload(
            Path("evidences") / str(settlement_id), upload_file
        )

    async def save_admin_application_file(
        self,
        application_id: uuid.UUID,
        upload_file: UploadFile,
    ) -> StoredFile:
        return await self._save_upload(
            Path("admin_applications") / str(application_id), upload_file
        )

    async def _save_upload(self, subdir: Path, upload_file: UploadFile) -> StoredFile:
        original_name = self.validate_filename(upload_file.filename or "upload.bin")
        suffix = Path(original_name).suffix.lower()
        file_bytes = await read_upload_within_limit(upload_file, self._max_upload_bytes)

        relative_path = subdir / f"{uuid.uuid4()}{suffix}"
        absolute_path = (self._root / relative_path).resolve()
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(file_bytes)

        return StoredFile(
            original_name=original_name,
            absolute_path=absolute_path,
            relative_path=relative_path,
            size=len(file_bytes),
            content_type=upload_file.content_type,
        )

    @staticmethod
    def validate_filename(filename: str) -> str:
        sanitized_name = Path(filename).name or "upload.bin"
        suffix = Path(sanitized_name).suffix.lower()
        if suffix not in SUPPORTED_EVIDENCE_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_EVIDENCE_SUFFIXES))
            raise ValueError(f"지원하지 않는 파일 형식입니다. 지원 확장자: {supported}")
        return sanitized_name

