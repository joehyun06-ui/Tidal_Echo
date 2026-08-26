#!/usr/bin/env python3
"""Compatibility launcher for canonical wake delivery.

The Supabase wake bridge still owns context, hard contact guards, wake-run
bookkeeping, and the historical ntfy mirror. Once that bridge accepts a
message, this launcher also writes the exact same message into the Render relay
that GuiTing actually reads. The relay endpoint is localhost-only in practice,
authenticated with the supervisor's per-process internal token, and deduplicates
by autonomous wake run id.

ntfy failure remains non-fatal: the user-visible source of truth is the
canonical relay, not the ntfy side channel.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import autonomous_wake_worker as worker


_original_bridge = worker._bridge


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
) -> dict[str, Any]:
    url, token = _relay_endpoint()
    payload = {
        "run_id": run_id,
        "message": message,
        "model": model,
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


async def _canonical_delivery_bridge(client, config, body: dict[str, Any]):
    op = body.get("op")
    if op != "deliver":
        return await _original_bridge(client, config, body)

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
        run_id=str(body.get("run_id") or ""),
        message=str(body.get("message") or ""),
        model=str(body.get("model") or ""),
    )
    if notification_failed:
        result["notification_failed"] = True
    result["canonical_relay"] = "delivered"
    return result


worker._bridge = _canonical_delivery_bridge


if __name__ == "__main__":
    raise SystemExit(worker.main())
