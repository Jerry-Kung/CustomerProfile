from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from src.config.settings import UploadConfig

_ALLOWED_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


class TaskUploadRejected(Exception):
    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _suffix_from_filename(name: str | None) -> str | None:
    if not name:
        return None
    suf = Path(name).suffix.lower()
    return suf if suf in _ALLOWED_EXT else None


def _suffix_from_content_type(ct: str | None) -> str | None:
    if not ct:
        return None
    m = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct.lower().split(";")[0].strip())
    return m


def _suffix_from_magic(head: bytes) -> str | None:
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def _pick_extension(*, filename: str | None, content_type: str | None, head: bytes) -> str:
    for candidate in (
        _suffix_from_filename(filename),
        _suffix_from_content_type(content_type),
        _suffix_from_magic(head),
    ):
        if candidate and candidate in _ALLOWED_EXT:
            return candidate
    raise TaskUploadRejected(
        code="UNSUPPORTED_MEDIA_TYPE",
        message="image must be jpg, png, webp, or gif (by extension, Content-Type, or file header)",
        status_code=415,
    )


class TaskUploadStore:
    """Persists optional task images under a directory shared with the worker (file_id mode)."""

    def __init__(self, config: UploadConfig) -> None:
        self._base = config.base_dir
        self._max = config.max_bytes_per_file
        self._base.mkdir(parents=True, exist_ok=True)

    async def save_optional(
        self,
        *,
        customer_phonenumber: str,
        upload: UploadFile | None,
        slot: str,
    ) -> str | None:
        if upload is None:
            return None
        data = await upload.read()
        if not data:
            return None
        if len(data) > self._max:
            raise TaskUploadRejected(
                code="PAYLOAD_TOO_LARGE",
                message=f"file exceeds TASK_UPLOAD_MAX_BYTES_PER_FILE ({self._max})",
                status_code=413,
            )
        ext = _pick_extension(
            filename=upload.filename,
            content_type=upload.content_type,
            head=data[:32],
        )
        safe_phone = re.sub(r"[^0-9+]", "_", customer_phonenumber)[:40] or "phone"
        fname = f"{safe_phone}_{slot}_{uuid.uuid4().hex}{ext}"
        dest = (self._base.resolve() / fname).resolve()
        dest.write_bytes(data)
        return str(dest)
