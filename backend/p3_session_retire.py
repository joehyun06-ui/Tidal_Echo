"""Fail-closed public lifecycle projection for retiring a Web Codex session.

The actual Codex store retirement remains owned by the existing Codex-aware
api-loop. This module only validates the durable Web-session authority around that
operation and sanitizes the browser-facing result/error contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from . import web_session_delete


RETIRE_FORBIDDEN = "web_session_retire_forbidden"
RETIRE_UNAVAILABLE = "web_session_retire_unavailable"
SESSION_NOT_FOUND = "web_session_not_found"
SESSION_DELETED = "web_session_deleted"
SESSION_ID_INVALID = "web_session_id_invalid"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class P3SessionRetireError(RuntimeError):
    def __init__(self, category: str, *, status_code: int = 503):
        super().__init__(category)
        self.category = category
        self.status_code = status_code


def safe_session_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_SESSION_ID.fullmatch(value) is None:
        raise P3SessionRetireError(SESSION_ID_INVALID, status_code=400)
    return value


def _session_rows(state: object) -> list[Mapping[str, object]]:
    if not isinstance(state, Mapping):
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    rows = state.get("sessions")
    if not isinstance(rows, list):
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    if any(not isinstance(row, Mapping) for row in rows):
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    return rows


def find_live_row(state: object, session_id: str) -> Mapping[str, object]:
    sid = safe_session_id(session_id)
    matches = [row for row in _session_rows(state) if row.get("id") == sid]
    if len(matches) > 1:
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    if not matches:
        raise P3SessionRetireError(SESSION_NOT_FOUND, status_code=404)
    return matches[0]


def require_codex_target(state: object, session_id: str) -> Mapping[str, object]:
    row = find_live_row(state, session_id)
    provider = row.get("provider")
    if provider == "api":
        raise P3SessionRetireError(RETIRE_FORBIDDEN, status_code=409)
    if provider != "codex":
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    return row


def raise_loop_retire_error(status_code: object, detail: object) -> None:
    """Translate only known localhost retire failures into stable public errors."""
    status = status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else 503
    category = ""
    if isinstance(detail, str) and len(detail) <= 512:
        try:
            parsed = json.loads(detail)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            candidate = parsed.get("error")
            if isinstance(candidate, str):
                category = candidate
    if status == 409 and category == "codex_generation_session_busy":
        raise P3SessionRetireError(web_session_delete.DELETE_JOB_ACTIVE, status_code=409)
    if status == 404 and category in {
        "codex_generation_session_not_found",
        "codex_canary_session_not_found",
    }:
        raise P3SessionRetireError(SESSION_NOT_FOUND, status_code=404)
    if status == 410 and category == SESSION_DELETED:
        raise P3SessionRetireError(SESSION_DELETED, status_code=410)
    raise P3SessionRetireError(RETIRE_UNAVAILABLE)


def project_retired(
    upstream: object,
    session_state_after: object,
    session_id: str,
) -> dict[str, object]:
    sid = safe_session_id(session_id)
    if not isinstance(upstream, Mapping) or upstream.get("ok") is not True:
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    retired = upstream.get("retired")
    if (
        not isinstance(retired, Mapping)
        or retired.get("api_session") != sid
        or retired.get("status") != "retired"
    ):
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    row = require_codex_target(session_state_after, sid)
    if row.get("delete_allowed") is not True:
        # Success is exposed only after the shared delete authority independently
        # confirms retirement and zero nonterminal jobs for this exact Codex row.
        raise P3SessionRetireError(RETIRE_UNAVAILABLE)
    return {
        "ok": True,
        "retired": {
            "id": sid,
            "provider": "codex",
            "status": "retired",
            "delete_allowed": True,
        },
    }
