#!/usr/bin/env python3
"""Compatibility launcher for canonical wake context and delivery.

The Supabase wake bridge remains responsible for phone-activity input, durable
wake-run bookkeeping, policy values, and the historical ntfy side channel.
GuiTing's real chat moved to the Render relay SQLite, so this launcher replaces
the stale Supabase recent-chat/idle fields with a read-only snapshot of the
currently active canonical relay session before each agent run. Accepted wake
messages are then written into that same relay session through an authenticated
localhost endpoint, which fans them out to GuiTing and deduplicates by wake run
id.

ntfy failure stays non-fatal: the canonical relay is the user-visible source of
truth, not the ntfy side channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import autonomous_wake_worker as worker


_original_bridge = worker._bridge
_run_min_idle_seconds: dict[str, int] = {}
_run_api_session: dict[str, str] = {}


def _relay_database_path() -> Path:
    raw = str(os.environ.get("RELAY_DB", "")).strip()
    path = Path(raw) if raw else worker.REPO_ROOT / "backend" / "relay.db"
    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError("canonical_relay_unavailable") from None
    if not resolved.is_file():
        raise RuntimeError("canonical_relay_unavailable")
    return resolved


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


def _active_api_session() -> str:
    try:
        from examples import api_loop
        active = str(api_loop.active_session_id() or "").strip()
    except Exception:
        raise RuntimeError("canonical_relay_unavailable") from None
    if len(active) > 128:
        raise RuntimeError("canonical_relay_unavailable")
    return active


def _canonical_context() -> dict[str, Any]:
    path = _relay_database_path()
    active_session = _active_api_session()
    conn = None
    try:
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if active_session:
            rows = conn.execute(
                """SELECT id,ts,direction,kind,text
                   FROM messages
                   WHERE direction IN ('in','out')
                     AND json_extract(meta, '$.api_session') = ?
                   ORDER BY id DESC LIMIT 120""",
                (active_session,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id,ts,direction,kind,text
                   FROM messages
                   WHERE direction IN ('in','out')
                     AND (json_extract(meta, '$.api_session') IS NULL
                          OR json_extract(meta, '$.api_session') = '')
                   ORDER BY id DESC LIMIT 120"""
            ).fetchall()
    except (OSError, sqlite3.Error):
        raise RuntimeError("canonical_relay_unavailable") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

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
        recent.append({
            "role": "user" if row["direction"] == "in" else "assistant",
            "content": text[:8000],
            "created_at": str(row["ts"] or ""),
        })

    recent.reverse()
    idle_seconds = 0
    if latest_user_at is not None:
        idle_seconds = max(
            0,
            int((datetime.now(timezone.utc) - latest_user_at).total_seconds()),
        )
    return {
        "api_session": active_session,
        "has_user_context": latest_user_at is not None,
        "idle_seconds": idle_seconds,
        "recent_messages": recent,
    }


def _relay_endpoint() -> tuple[str, str]:
    token = str(os.environ.get("API_LOOP_INTERNAL_TOKEN", "")).strip()
    raw_port = str(os.environ.get("RELAY_PORT", "")).strip()
    if len(token) < 32:
        raise RuntimeError("canonical_relay_unavailable")
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise RuntimeError("canonical_relay_unavailable") from None
    if not 1 <= port <= 65535:
        raise RuntimeError("canonical_relay_unavailable")
    return f"http://127.0.0.1:{port}/internal/autonomous-wake/out", token


async def _deliver_canonical_relay(
    client,
    *,
    run_id: str,
    message: str,
    model: str,
    api_session: str,
) -> dict[str, Any]:
    url, token = _relay_endpoint()
    payload = {
        "run_id": run_id,
        "message": message,
        "model": model,
        "api_session": api_session,
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.post(
                url,
                headers={"X-Internal-Token": token, "Content-Type": "application/json"},
                json=payload,
            )
            raw = await response.aread()
            if len(raw) > 64 * 1024:
                raise RuntimeError("canonical_relay_unavailable")
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError):
                raise RuntimeError("canonical_relay_unavailable") from None
            if (
                response.status_code < 200
                or response.status_code >= 300
                or not isinstance(data, dict)
                or data.get("status") != "delivered"
            ):
                raise RuntimeError("canonical_relay_unavailable")
            return data
        except Exception as error:
            last_error = error
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
    raise RuntimeError("canonical_relay_unavailable") from last_error


async def _mark_bridge_failed(client, config, run_id: str, category: str) -> None:
    try:
        await _original_bridge(
            client,
            config,
            {"op": "fail", "run_id": run_id, "category": category},
        )
    except Exception:
        pass


def _clear_run(run_id: str) -> None:
    _run_min_idle_seconds.pop(run_id, None)
    _run_api_session.pop(run_id, None)


async def _canonical_delivery_bridge(client, config, body: dict[str, Any]):
    op = body.get("op")
    run_id = str(body.get("run_id") or "")

    if op == "prepare":
        result = await _original_bridge(client, config, body)
        if result.get("status") != "ready" or result.get("duplicate") is True:
            return result
        context = _canonical_context()
        if not context["has_user_context"]:
            await _mark_bridge_failed(client, config, run_id, "no_canonical_user_context")
            return {"ok": True, "status": "silent", "reason": "no_canonical_user_context"}
        result["recent_messages"] = context["recent_messages"]
        result["idle_seconds"] = context["idle_seconds"]
        try:
            min_idle = int(result.get("min_contact_idle_seconds") or 0)
        except (TypeError, ValueError):
            min_idle = 0
        _run_min_idle_seconds[run_id] = max(0, min(min_idle, 86400))
        _run_api_session[run_id] = str(context["api_session"] or "")
        return result

    if op in {"silent", "fail"}:
        try:
            return await _original_bridge(client, config, body)
        finally:
            _clear_run(run_id)

    if op != "deliver":
        return await _original_bridge(client, config, body)

    try:
        context = _canonical_context()
        min_idle = _run_min_idle_seconds.get(run_id, 0)
        prepared_session = _run_api_session.get(run_id, "")
        current_session = str(context["api_session"] or "")
        if current_session != prepared_session:
            await _mark_bridge_failed(client, config, run_id, "active_session_changed")
            raise RuntimeError("active_session_changed")
        if (
            not context["has_user_context"]
            or int(context["idle_seconds"]) < min_idle
        ):
            await _mark_bridge_failed(client, config, run_id, "recent_user_activity")
            raise RuntimeError("recent_user_activity")

        notification_failed = False
        try:
            result = await _original_bridge(client, config, body)
        except RuntimeError as error:
            if str(error) != "notification_failed":
                raise
            notification_failed = True
            worker._safe_log("notification_failed")
            result = {
                "ok": True,
                "status": "delivered",
                "notification_failed": True,
            }

        if result.get("status") != "delivered":
            return result

        await _deliver_canonical_relay(
            client,
            run_id=run_id,
            message=str(body.get("message") or ""),
            model=str(body.get("model") or ""),
            api_session=prepared_session,
        )
        if notification_failed:
            result["notification_failed"] = True
        result["canonical_relay"] = "delivered"
        return result
    finally:
        _clear_run(run_id)


worker._bridge = _canonical_delivery_bridge


if __name__ == "__main__":
    raise SystemExit(worker.main())
