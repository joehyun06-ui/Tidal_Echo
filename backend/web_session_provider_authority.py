"""Durable per-Web-session generation-provider authority for P3-A.

The api-loop config file is already the durable authority for Web session identity,
creation order, titles, and the active-session pointer.  P3-A extends only that
existing session record with an immutable ``provider`` field.

Safety rules:
- missing provider on a pre-P3 row means ``api`` for backward compatibility;
- a present provider must be exactly ``api`` or ``codex``;
- provider is immutable after session publication;
- callers must still cross-check Codex rows against the durable Codex generation
  store before dispatch.  This module never treats a UI title or ``pinned`` bit as
  provider authority.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Mapping
from typing import Any


API_PROVIDER = "api"
CODEX_PROVIDER = "codex"
PROVIDERS = frozenset({API_PROVIDER, CODEX_PROVIDER})
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class WebSessionProviderAuthorityError(RuntimeError):
    """Fixed, data-free session-authority failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<WebSessionProviderAuthorityError category={self.category!r}>"


def normalize_provider(value: object, *, missing_means_api: bool = False) -> str:
    if value is None and missing_means_api:
        return API_PROVIDER
    if not isinstance(value, str) or value not in PROVIDERS:
        raise WebSessionProviderAuthorityError("web_session_provider_invalid")
    return value


def _safe_session_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_SESSION_ID.fullmatch(value) is None:
        raise WebSessionProviderAuthorityError("web_session_id_invalid")
    return value


def _safe_title(value: object, fallback: str = "New chat") -> str:
    if value is None:
        value = fallback
    if not isinstance(value, str):
        raise WebSessionProviderAuthorityError("web_session_title_invalid")
    title = value.strip()
    if not title:
        title = fallback
    if len(title) > 120:
        raise WebSessionProviderAuthorityError("web_session_title_invalid")
    return title


def _safe_since_id(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise WebSessionProviderAuthorityError("web_session_since_id_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise WebSessionProviderAuthorityError("web_session_since_id_invalid") from None
    if result < 0:
        raise WebSessionProviderAuthorityError("web_session_since_id_invalid")
    return result


def _safe_created_at(value: object) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str) or len(value) > 80:
        raise WebSessionProviderAuthorityError("web_session_created_at_invalid")
    return value


class WebSessionProviderAuthority:
    """Read/write provider-aware Web sessions through the legacy api-loop config."""

    def __init__(self, legacy) -> None:
        self.legacy = legacy

    def _raw_rows(self) -> list[Mapping[str, object]]:
        rows = self.legacy.load_config().get("sessions")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise WebSessionProviderAuthorityError("web_session_authority_invalid")
        out: list[Mapping[str, object]] = []
        for item in rows:
            if not isinstance(item, dict):
                raise WebSessionProviderAuthorityError("web_session_authority_invalid")
            out.append(item)
        return out

    def normalize_row(self, item: Mapping[str, object]) -> dict[str, Any]:
        if "id" not in item:
            raise WebSessionProviderAuthorityError("web_session_authority_invalid")
        row: dict[str, Any] = {
            "id": _safe_session_id(item.get("id")),
            "title": _safe_title(item.get("title")),
            "since_id": _safe_since_id(item.get("since_id")),
            "created_at": _safe_created_at(item.get("created_at")),
            "pinned": bool(item.get("pinned", False)),
            "provider": normalize_provider(
                item.get("provider"),
                missing_means_api="provider" not in item,
            ),
        }
        return row

    def session_rows(self) -> list[dict[str, Any]]:
        rows = [self.normalize_row(item) for item in self._raw_rows()]
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise WebSessionProviderAuthorityError("web_session_authority_invalid")
        return rows

    def row_for_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        session_id = _safe_session_id(session_id)
        for row in self.session_rows():
            if row["id"] == session_id:
                return row
        return None

    def provider_for_session(self, session_id: str) -> str:
        """Return API for non-Web/unknown sessions; known Web rows carry authority."""
        row = self.row_for_session(session_id) if session_id else None
        return str(row["provider"]) if row is not None else API_PROVIDER

    def active_session_id(self) -> str:
        cfg = self.legacy.load_config()
        active = str(cfg.get("active_session") or "").strip()
        rows = self.session_rows()
        ids = {row["id"] for row in rows}
        if active in ids:
            return active
        return rows[-1]["id"] if rows else ""

    def sessions_public(self) -> dict[str, Any]:
        return {
            "active_session": self.active_session_id(),
            "sessions": self.session_rows(),
        }

    def save_sessions(
        self,
        rows: list[Mapping[str, object]],
        active: str | None = None,
    ) -> dict[str, Any]:
        normalized = [self.normalize_row(item) for item in rows]
        ids = [row["id"] for row in normalized]
        if len(ids) != len(set(ids)):
            raise WebSessionProviderAuthorityError("web_session_authority_invalid")
        if active is not None:
            active = _safe_session_id(active)
            if active not in set(ids):
                raise WebSessionProviderAuthorityError("web_session_active_invalid")
        cfg = self.legacy.load_config()
        cfg["sessions"] = normalized
        if active is not None:
            cfg["active_session"] = active
        self.legacy.save_config(cfg)
        return self.sessions_public()

    def new_row(
        self,
        *,
        title: str = "New chat",
        since_id: int = 0,
        provider: str = API_PROVIDER,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        provider = normalize_provider(provider)
        sid = session_id or (
            "api-"
            + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:4]
        )
        return {
            "id": _safe_session_id(sid),
            "title": _safe_title(title),
            "since_id": _safe_since_id(since_id),
            "created_at": self.legacy.now_iso(),
            "pinned": False,
            "provider": provider,
        }

    def create_api_session(
        self,
        *,
        title: str = "New chat",
        since_id: int = 0,
        activate: bool = True,
    ) -> dict[str, Any]:
        rows = self.session_rows()
        row = self.new_row(
            title=title,
            since_id=since_id,
            provider=API_PROVIDER,
        )
        rows.append(row)
        self.save_sessions(rows, row["id"] if activate else None)
        return row

    def publish_row(self, row: Mapping[str, object], *, activate: bool) -> dict[str, Any]:
        normalized = self.normalize_row(row)
        rows = self.session_rows()
        if any(item["id"] == normalized["id"] for item in rows):
            raise WebSessionProviderAuthorityError("web_session_conflict")
        rows.append(normalized)
        self.save_sessions(rows, normalized["id"] if activate else None)
        return normalized

    def patch_session(
        self,
        session_id: str,
        body: Mapping[str, object],
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        if not isinstance(body, dict):
            raise WebSessionProviderAuthorityError("web_session_patch_invalid")
        if "provider" in body:
            raise WebSessionProviderAuthorityError("web_session_provider_immutable")
        if set(body) - {"title", "pinned", "active"}:
            raise WebSessionProviderAuthorityError("web_session_patch_invalid")
        rows = self.session_rows()
        found = False
        for item in rows:
            if item["id"] != session_id:
                continue
            found = True
            if "title" in body:
                item["title"] = _safe_title(body.get("title"), item["title"])
            if "pinned" in body:
                item["pinned"] = bool(body.get("pinned"))
        if not found:
            raise WebSessionProviderAuthorityError("web_session_not_found")
        return self.save_sessions(rows, session_id if body.get("active") else None)
