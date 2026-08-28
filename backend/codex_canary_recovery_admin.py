"""Frozen-canary admin action for recovering an already-dispatched Codex turn.

The route in this module is installed only by the alternate canary relay entrypoint.
It never starts Codex and never creates a turn.  It can only re-arm the latest job
when all durable evidence says one turn already existed but delivery failed because
no final answer was observed.  Generation must be explicitly disabled first.
"""

from __future__ import annotations

import os
import re
from contextlib import closing
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from . import codex_generation_store as store
from .codex_generation_runtime_config import load_generation_runtime_config


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ARMED_CATEGORY = "codex_generation_recovery_armed"


class CodexCanaryRecoveryAdminError(RuntimeError):
    def __init__(self, category: str, *, status_code: int = 409):
        super().__init__(category)
        self.category = category
        self.status_code = status_code


def _session_id(value: object) -> str:
    if not isinstance(value, str) or _SESSION_ID.fullmatch(value) is None:
        raise CodexCanaryRecoveryAdminError("invalid_canary_request", status_code=400)
    return value


def _store_path() -> Path:
    persistent_root = Path(os.environ.get("RENDER_PERSISTENT_ROOT", "/var/data"))
    if not persistent_root.is_absolute() or ".." in persistent_root.parts:
        raise CodexCanaryRecoveryAdminError("codex_canary_recovery_unavailable", status_code=503)
    try:
        return load_generation_runtime_config(
            os.environ,
            persistent_root=persistent_root,
        ).store_path
    except Exception:
        raise CodexCanaryRecoveryAdminError(
            "codex_canary_recovery_unavailable",
            status_code=503,
        ) from None


def _require_frozen_generation() -> None:
    if os.environ.get("CODEX_GENERATION_ENABLED", "false") != "false":
        raise CodexCanaryRecoveryAdminError(
            "codex_canary_recovery_requires_disabled_generation",
            status_code=409,
        )


def _arm_existing_completion_recovery(expected_session: str) -> dict[str, object]:
    """Re-arm one failed existing turn for reconciliation, never for dispatch."""
    _require_frozen_generation()
    database = _store_path()
    try:
        with closing(store.connect(database)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                """SELECT status,thread_id FROM codex_sessions WHERE api_session=?""",
                (expected_session,),
            ).fetchone()
            if session is None:
                conn.execute("ROLLBACK")
                raise CodexCanaryRecoveryAdminError(
                    "codex_canary_session_not_found",
                    status_code=404,
                )
            if session["status"] != "active" or session["thread_id"] is None:
                conn.execute("ROLLBACK")
                raise CodexCanaryRecoveryAdminError(
                    "codex_canary_recovery_not_eligible",
                    status_code=409,
                )
            job = conn.execute(
                """SELECT id,status,attempt_count,recovery_count,turn_id,
                          assistant_message_id,error_category
                   FROM codex_generation_jobs
                   WHERE api_session=? ORDER BY id DESC LIMIT 1""",
                (expected_session,),
            ).fetchone()
            if job is None:
                conn.execute("ROLLBACK")
                raise CodexCanaryRecoveryAdminError(
                    "codex_canary_recovery_not_eligible",
                    status_code=409,
                )
            already_armed = (
                job["status"] == "dispatch_uncertain"
                and job["error_category"] == _ARMED_CATEGORY
                and job["turn_id"] is not None
                and job["assistant_message_id"] is None
            )
            eligible = (
                job["status"] == "failed"
                and job["error_category"] == "codex_generation_empty_response"
                and job["turn_id"] is not None
                and job["assistant_message_id"] is None
                and isinstance(job["attempt_count"], int)
                and not isinstance(job["attempt_count"], bool)
                and job["attempt_count"] >= 1
            )
            if not already_armed and not eligible:
                conn.execute("ROLLBACK")
                raise CodexCanaryRecoveryAdminError(
                    "codex_canary_recovery_not_eligible",
                    status_code=409,
                )
            if eligible:
                updated = conn.execute(
                    """UPDATE codex_generation_jobs
                       SET status='dispatch_uncertain',lease_until=NULL,recovery_count=0,
                           error_category=?,updated_at=?
                       WHERE id=? AND status='failed'
                         AND error_category='codex_generation_empty_response'
                         AND turn_id IS NOT NULL AND assistant_message_id IS NULL""",
                    (_ARMED_CATEGORY, store.now_iso(), job["id"]),
                )
                if updated.rowcount != 1:
                    conn.execute("ROLLBACK")
                    raise CodexCanaryRecoveryAdminError(
                        "codex_canary_recovery_unavailable",
                        status_code=503,
                    )
                job = conn.execute(
                    """SELECT id,status,attempt_count,recovery_count,turn_id,
                              assistant_message_id,error_category
                       FROM codex_generation_jobs WHERE id=?""",
                    (job["id"],),
                ).fetchone()
            conn.execute("COMMIT")
    except CodexCanaryRecoveryAdminError:
        raise
    except Exception:
        raise CodexCanaryRecoveryAdminError(
            "codex_canary_recovery_unavailable",
            status_code=503,
        ) from None
    return {
        "ok": True,
        "provider": "codex",
        "recovery": {
            "api_session": expected_session,
            "status": "armed",
            "attempt_count": int(job["attempt_count"]),
            "recovery_count": int(job["recovery_count"]),
            "turn_bound": True,
            "assistant_message_bound": False,
        },
    }


def _error(exc: CodexCanaryRecoveryAdminError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": exc.category},
        status_code=exc.status_code,
    )


def install(relay_module) -> None:
    if getattr(relay_module, "_CODEX_CANARY_RECOVERY_ADMIN_INSTALLED", False):
        return

    @relay_module.app.post("/provider/canary/{session_id}/recover-existing")
    async def recover_existing(session_id: str, request: Request):
        relay_module.check_auth(request)
        try:
            return _arm_existing_completion_recovery(_session_id(session_id))
        except CodexCanaryRecoveryAdminError as exc:
            return _error(exc)

    relay_module._CODEX_CANARY_RECOVERY_ADMIN_INSTALLED = True
