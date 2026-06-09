"""Upload size-cap guard — buffered reads must abort past the limit."""

from __future__ import annotations

import io

import pytest
from fastapi import UploadFile

from union_ledger.services.file_storage import (
    FileTooLargeError,
    read_upload_within_limit,
)


async def test_read_upload_within_limit_accepts_under_cap() -> None:
    upload = UploadFile(filename="ok.png", file=io.BytesIO(b"x" * 500))
    data = await read_upload_within_limit(upload, max_bytes=1000)
    assert data == b"x" * 500


async def test_read_upload_within_limit_rejects_over_cap() -> None:
    upload = UploadFile(filename="big.png", file=io.BytesIO(b"x" * 5000))
    with pytest.raises(FileTooLargeError):
        await read_upload_within_limit(upload, max_bytes=1000)
