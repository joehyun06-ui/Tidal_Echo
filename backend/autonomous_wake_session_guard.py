"""Fail-closed routing guard for autonomous wake chat delivery.

Autonomous wake is an ordinary/API surface.  It must never inherit whichever Web
session happens to be active, and it must never target an active Codex-pinned
session.  The Codex generation store is the authority for that distinction; UI
titles and the API-loop presentation-level ``pinned`` field are deliberately not
used here.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from . import codex_generation_store


_API_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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


def is_active_codex_session(
    api_session: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether ``api_session`` is an active Codex session.

    When Codex generation is enabled, inability to read its authority store is a
    hard failure instead of being interpreted as "not Codex".
    """
    env = os.environ if environ is None else environ
    session = str(api_session or "").strip()
    if not session:
        return False
    if _API_SESSION_RE.fullmatch(session) is None:
        raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")

    store_path = _store_path(env)
    if not store_path.is_file():
        if _generation_enabled(env):
            raise AutonomousWakeSessionError(
                "autonomous_wake_session_guard_unavailable"
            )
        return False

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
    return bool(
        row is not None
        and row.get("provider") == "codex"
        and row.get("status") == "active"
    )


def select_wake_api_session(
    sessions: Iterable[Mapping[str, object]],
    environ: Mapping[str, str] | None = None,
) -> str:
    """Choose the stable ordinary/API session used by autonomous wake.

    ``AUTONOMOUS_WAKE_API_SESSION`` may explicitly pin the target.  Without an
    override, API-loop session order is used as a stable primary-session order:
    the first session that is not active in the Codex authority store wins.  If
    no ordinary API session exists, the untagged legacy surface (``""``) is used.
    The current active Web session is never consulted.
    """
    env = os.environ if environ is None else environ
    explicit = str(env.get("AUTONOMOUS_WAKE_API_SESSION", "")).strip()
    if explicit and _API_SESSION_RE.fullmatch(explicit) is None:
        raise AutonomousWakeSessionError("autonomous_wake_target_invalid")

    session_ids: list[str] = []
    ordinary: list[str] = []
    for item in sessions:
        if not isinstance(item, Mapping):
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        session = str(item.get("id") or "").strip()
        if _API_SESSION_RE.fullmatch(session) is None:
            raise AutonomousWakeSessionError("autonomous_wake_session_guard_unavailable")
        session_ids.append(session)
        if not is_active_codex_session(session, env):
            ordinary.append(session)

    if explicit:
        if explicit not in session_ids:
            raise AutonomousWakeSessionError("autonomous_wake_target_invalid")
        if is_active_codex_session(explicit, env):
            raise AutonomousWakeSessionError("autonomous_wake_codex_session_forbidden")
        return explicit

    return ordinary[0] if ordinary else ""
