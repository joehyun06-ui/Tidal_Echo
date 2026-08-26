"""Canonical relay context for autonomous wake.

The wake scheduler still uses the Supabase bridge for phone activity, durable
wake-run bookkeeping, and its existing policy settings. GuiTing's actual chat
history, however, lives in the Render relay SQLite. This module exposes only a
localhost-authenticated snapshot of that canonical history so the wake model's
recent conversation and idle calculation cannot drift to the old Supabase chat.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse


def _error(status_code: int, category: str) -> JSONResponse:
    return JSONResponse({"error": category}, status_code=status_code)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def install(app, relay_app) -> None:
    @app.post("/internal/autonomous-wake/context")
    async def autonomous_wake_context(request: Request):
        expected = str(relay_app.API_LOOP_INTERNAL_TOKEN or "").strip()
        supplied = str(request.headers.get("x-internal-token") or "").strip()
        if len(expected) < 32 or not supplied or not hmac.compare_digest(supplied, expected):
            return _error(401, "unauthorized")

        with relay_app.db() as conn:
            rows = conn.execute(
                """SELECT id,ts,direction,kind,text,meta
                   FROM messages
                   WHERE direction IN ('in','out')
                   ORDER BY id DESC LIMIT 120"""
            ).fetchall()

        recent: list[dict[str, str]] = []
        latest_user_at: datetime | None = None
        for row in rows:
            if latest_user_at is None and row["direction"] == "in":
                latest_user_at = _parse_utc(row["ts"])
            if len(recent) >= 24:
                continue
            text = str(row["text"] or "").strip()
            if not text:
                continue
            kind = str(row["kind"] or "")
            if kind in {"thinking", "thinking_delta", "reply_delta", "act"}:
                continue
            role = "user" if row["direction"] == "in" else "assistant"
            recent.append({
                "role": role,
                "content": text[:8000],
                "created_at": str(row["ts"] or ""),
            })

        recent.reverse()
        now = datetime.now(timezone.utc)
        idle_seconds = (
            max(0, int((now - latest_user_at).total_seconds()))
            if latest_user_at is not None
            else 0
        )

        return {
            "ok": True,
            "has_user_context": latest_user_at is not None,
            "idle_seconds": idle_seconds,
            "recent_messages": recent,
        }
