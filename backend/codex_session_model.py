"""Change a Codex session's model between turns without changing its identity."""

from __future__ import annotations

import re
from contextlib import closing

from fastapi import HTTPException, Request

from . import codex_generation_store as store
from .codex_generation_protocol import CodexGenerationError
from .provider_settings import fail, read_body


def valid_session_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", value):
        fail("provider_settings_invalid")
    return value


def public_selection(row):
    if not row or row.get("provider") != "codex" or row.get("status") != "active":
        fail("codex_session_unavailable")
    return {"session_id": row["api_session"], "model": row["model"],
            "reasoning_effort": row.get("reasoning_effort"), "effective": "next_message"}


def save_selection(path, session_id, selection):
    valid_session_id(session_id)
    with closing(store.connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM codex_sessions WHERE api_session=?", (session_id,)).fetchone()
            public_selection(dict(row) if row else None)
            busy = conn.execute(
                "SELECT 1 FROM codex_generation_jobs WHERE api_session=? AND status NOT IN ('completed','failed') LIMIT 1",
                (session_id,),
            ).fetchone()
            if busy:
                fail("codex_generation_busy")
            conn.execute(
                "UPDATE codex_sessions SET model=?,reasoning_effort=?,updated_at=? WHERE api_session=?",
                (selection.model, selection.reasoning_effort, store.now_iso(), session_id),
            )
            row = dict(conn.execute("SELECT * FROM codex_sessions WHERE api_session=?", (session_id,)).fetchone())
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True, **public_selection(row)}


def install(app, legacy, runtime, integration):
    def require_session(session_id):
        valid_session_id(session_id)
        if not runtime.generation_enabled:
            fail("codex_generation_disabled")
        if integration.provider_for_session(session_id) != "codex":
            fail("codex_session_unavailable")
        return public_selection(store.get_session(runtime.config.store_path, session_id))

    @app.get("/loop/sessions/{session_id}/model")
    async def get_model(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        try:
            return require_session(session_id)
        except HTTPException:
            raise
        except Exception:
            fail("codex_session_unavailable")

    @app.post("/loop/sessions/{session_id}/model")
    async def set_model(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        body = await read_body(request)
        if (not isinstance(body, dict) or set(body) - {"model", "reasoning_effort"}
                or not isinstance(body.get("model"), str) or not body["model"]
                or (body.get("reasoning_effort") is not None and not isinstance(body["reasoning_effort"], str))):
            fail("provider_settings_invalid")
        try:
            require_session(session_id)
            async with runtime.foundation.activity_gate.control():
                selection = await runtime.foundation.generation.qualify(body["model"], body.get("reasoning_effort"))
                return save_selection(runtime.config.store_path, session_id, selection)
        except HTTPException:
            raise
        except CodexGenerationError as exc:
            if exc.category == "codex_generation_busy":
                fail("codex_generation_busy")
            if exc.category in {"codex_generation_model_unavailable", "codex_generation_effort_invalid"}:
                fail("codex_model_invalid")
            fail("provider_settings_unavailable")
        except Exception:
            fail("codex_session_unavailable")
