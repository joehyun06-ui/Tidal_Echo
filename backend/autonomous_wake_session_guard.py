"""Fail-closed routing guard for autonomous wake chat delivery.

Autonomous wake is an ordinary/API surface. It must never inherit whichever Web
session happens to be active and, under P3, it must never target any Web session
whose durable provider authority is Codex -- even after that Codex session has been
retired. Missing pre-P3 provider fields are reconciled only against durable Codex
store history; UI titles and presentation-level ``pinned`` metadata are never used.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Iterable, Mapping

from . import codex_generation_store


_API_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOOP_CONFIG_MAX_BYTES = 1024 * 1024


class AutonomousWakeSessionError(RuntimeError):
    """Fixed, data-free autonomous-wake routing failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _generation_enabled(environ: Mapping[str, str]) -> bool:
    raw = str(environ.get("CODEX_GENERATION_ENABLED", "false"))
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")


def _store_path(environ: Mapping[str, str]) -> Path:
    raw = str(
        environ.get("CODEX_GENERATION_DB", "/var/data/codex-generation.db")
    ).strip()
    try:
        path = Path(raw)
    except (TypeError, ValueError):
        raise AutonomousWakeSessionError(
            "autonomous_wake_session_guard_unavailable"
        ) from None
    if not raw or not path.is_absolute() or ".." in path.parts:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    return path


def _regular_file_size(path: Path, *, missing_ok: bool) -> int | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise AutonomousWakeSessionError(
            "autonomous_wake_session_guard_unavailable"
        ) from None
    except OSError:
        raise AutonomousWakeSessionError(
            "autonomous_wake_session_guard_unavailable"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    return int(info.st_size)


def _loop_provider_map(environ: Mapping[str, str]) -> dict[str, str | None]:
    """Read only session id and explicit provider from durable loop config."""
    raw = str(environ.get("LOOP_CONFIG", "")).strip()
    if not raw:
        return {}
    try:
        path = Path(raw)
    except (TypeError, ValueError):
        raise AutonomousWakeSessionError(
            "autonomous_wake_session_guard_unavailable"
        ) from None
    if not path.is_absolute() or ".." in path.parts:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    size = _regular_file_size(path, missing_ok=True)
    if size is None:
        return {}
    if size <= 0 or size > _LOOP_CONFIG_MAX_BYTES:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AutonomousWakeSessionError(
            "autonomous_wake_session_guard_unavailable"
        ) from None
    if not isinstance(payload, dict):
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    rows = payload.get("sessions", [])
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    result: dict[str, str | None] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        session = str(item.get("id") or "").strip()
        if _API_SESSION_RE.fullmatch(session) is None or session in result:
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        provider = item.get("provider") if "provider" in item else None
        if provider is not None and provider not in {"api", "codex"}:
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        result[session] = str(provider) if provider is not None else None
    return result


def _codex_session(
    api_session: str,
    environ: Mapping[str, str],
) -> Mapping[str, object] | None:
    session = str(api_session or "").strip()
    if not session:
        return None
    if _API_SESSION_RE.fullmatch(session) is None:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    store_path = _store_path(environ)
    size = _regular_file_size(store_path, missing_ok=True)
    if size is None:
        if _generation_enabled(environ):
            raise AutonomousWakeSessionError(
                "autonomous_wake_session_guard_unavailable"
            )
        return None
    if size <= 0:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    try:
        row = codex_generation_store.get_session(store_path, session)
    except (
        OSError,
        sqlite3.Error,
        codex_generation_store.CodexGenerationStoreError,
    ):
        raise AutonomousWakeSessionError(
            "autonomous_wake_session_guard_unavailable"
        ) from None
    if row is not None and row.get("provider") != "codex":
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    return row


def is_active_codex_session(
    api_session: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether ``api_session`` is active in the Codex generation store."""
    env = os.environ if environ is None else environ
    row = _codex_session(api_session, env)
    return bool(row is not None and row.get("status") == "active")


def is_codex_web_session(
    api_session: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether autonomous wake must treat a Web session as Codex forever.

    This is the final-persistence-boundary classifier. An explicit durable
    ``provider=codex`` row is sufficient to forbid wake delivery. A pre-P3 row with
    no provider is Codex when the generation store proves historical Codex ownership
    (active or retired). Explicit API authority conflicting with Codex history fails
    closed instead of being interpreted as ordinary API.
    """
    env = os.environ if environ is None else environ
    session = str(api_session or "").strip()
    if not session:
        return False
    if _API_SESSION_RE.fullmatch(session) is None:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")

    provider = _loop_provider_map(env).get(session)
    if provider == "codex":
        return True

    historical_codex = _codex_session(session, env) is not None
    if provider == "api" and historical_codex:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
    return historical_codex


def select_wake_api_session(
    sessions: Iterable[Mapping[str, object]],
    environ: Mapping[str, str] | None = None,
) -> str:
    """Choose the stable ordinary/API session used by autonomous wake.

    ``AUTONOMOUS_WAKE_API_SESSION`` may explicitly pin the target. Without an
    override, API-loop creation order is used. Explicit ``provider=codex`` is never
    ordinary. A pre-P3 row with no provider becomes Codex only when the durable
    Codex store proves that exact session existed there (active or retired).
    Explicit API authority conflicting with any Codex-store history fails closed.
    If no ordinary API session exists, the untagged legacy surface (``""``) is used.
    """
    env = os.environ if environ is None else environ
    explicit = str(env.get("AUTONOMOUS_WAKE_API_SESSION", "")).strip()
    if explicit and _API_SESSION_RE.fullmatch(explicit) is None:
        raise AutonomousWakeSessionError("autonomous_wake_target_invalid")

    config_providers = _loop_provider_map(env)
    session_ids: list[str] = []
    providers: dict[str, str] = {}
    ordinary: list[str] = []
    for item in sessions:
        if not isinstance(item, Mapping):
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        session = str(item.get("id") or "").strip()
        if _API_SESSION_RE.fullmatch(session) is None:
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        session_ids.append(session)
        row_provider = item.get("provider") if "provider" in item else None
        if row_provider is not None and row_provider not in {"api", "codex"}:
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        configured_provider = config_providers.get(session)
        if (
            row_provider is not None
            and configured_provider is not None
            and row_provider != configured_provider
        ):
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")

        explicit_provider = row_provider or configured_provider
        historical_codex = _codex_session(session, env) is not None
        if explicit_provider == "api" and historical_codex:
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        provider = str(
            explicit_provider
            or ("codex" if historical_codex else "api")
        )
        providers[session] = provider
        if provider == "api":
            ordinary.append(session)

    if explicit:
        if explicit not in session_ids:
            raise AutonomousWakeSessionError("autonomous_wake_target_invalid")
        if providers.get(explicit) == "codex":
            raise AutonomousWakeSessionError("autonomous_wake_codex_session_forbidden")
        return explicit

    return ordinary[0] if ordinary else ""
