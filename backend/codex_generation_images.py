"""Bounded, canonical image inputs for the production Codex Web runtime.

Only relay-created local uploads are read. No URL is fetched and no credential is
passed to Codex. LocalImage keeps the JSONL request below the transport limit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .codex_canary_ingress import CodexCanaryIngressError, read_web_message

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 4
_UPLOAD = re.compile(r"^/(?:[A-Za-z0-9_-]+/)*uploads/(att-[A-Za-z0-9_-]{14}\.[A-Za-z0-9]{1,8})$")


@dataclass(frozen=True)
class ImageInput:
    path: Path = field(repr=False)
    sha256: str
    mime: str
    size: int


@dataclass(frozen=True)
class ImageMessageInput:
    text: str = field(repr=False)
    images: tuple[ImageInput, ...]

    def digest(self) -> str:
        if not isinstance(self.text, str) or len(self.text) > 1_048_576 or not 1 <= len(self.images) <= MAX_IMAGES:
            raise CodexCanaryIngressError("codex_generation_input_invalid")
        payload = {"version": 1, "text": self.text, "images": [
            {"name": image.path.name, "sha256": image.sha256, "mime": image.mime, "size": image.size}
            for image in self.images
        ]}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def wire_items(self) -> list[dict]:
        return ([{"type": "text", "text": self.text}] if self.text else []) + [
            {"type": "localImage", "path": str(image.path)} for image in self.images
        ]


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 13:
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise CodexCanaryIngressError("codex_image_format_unsupported")


def _read_image(attachment: object, upload_dir: Path) -> ImageInput:
    if not isinstance(attachment, dict) or attachment.get("kind") != "image":
        raise CodexCanaryIngressError("codex_image_format_unsupported")
    raw = attachment.get("url")
    if not isinstance(raw, str) or len(raw) > 512:
        raise CodexCanaryIngressError("codex_image_reference_invalid")
    try:
        url = urlsplit(raw)
    except ValueError:
        raise CodexCanaryIngressError("codex_image_reference_invalid") from None
    match = _UPLOAD.fullmatch(url.path)
    if raw != url.path or url.scheme or url.netloc or url.query or url.fragment or not match:
        raise CodexCanaryIngressError("codex_image_reference_invalid")
    path = upload_dir.resolve() / match.group(1)
    try:
        # O_NOFOLLOW rejects symlinks; fstat verifies the opened file, not a
        # client-supplied path. Read one bounded buffer for validation + hashing.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CodexCanaryIngressError("codex_image_reference_invalid")
            if not 0 < info.st_size <= MAX_IMAGE_BYTES:
                raise CodexCanaryIngressError("codex_image_too_large")
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        raise CodexCanaryIngressError("codex_image_unavailable") from None
    if len(data) != info.st_size:
        raise CodexCanaryIngressError("codex_image_changed")
    mime = _mime(data)
    if attachment.get("mime") != mime or attachment.get("size") != len(data):
        raise CodexCanaryIngressError("codex_image_metadata_invalid")
    return ImageInput(path, hashlib.sha256(data).hexdigest(), mime, len(data))


def load_image_web_message(
    relay_db: str | Path, *, canonical_message_id: int, api_session: str,
    upload_dir: Path, expected_text: str | None = None, expected_digest: str | None = None,
) -> str | ImageMessageInput:
    text, meta = read_web_message(relay_db, canonical_message_id=canonical_message_id, api_session=api_session)
    if not isinstance(text, str) or len(text) > 1_048_576:
        raise CodexCanaryIngressError("codex_canary_text_invalid")
    if expected_text is not None and text != expected_text:
        raise CodexCanaryIngressError("codex_canary_input_contract_changed")
    attachments = meta.get("attachments") or []
    if not isinstance(attachments, list) or len(attachments) > MAX_IMAGES:
        raise CodexCanaryIngressError("codex_image_count_invalid")
    if not text and not attachments:
        raise CodexCanaryIngressError("codex_canary_text_invalid")
    value = ImageMessageInput(text, tuple(_read_image(item, upload_dir) for item in attachments)) if attachments else text
    digest = value.digest() if isinstance(value, ImageMessageInput) else hashlib.sha256(text.encode()).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise CodexCanaryIngressError("codex_canary_input_contract_changed")
    return value
