#!/usr/bin/env python3
"""Compatibility launcher that treats ntfy failure as non-fatal delivery.

The authenticated wake bridge persists the autonomous assistant message before
attempting ntfy. Production DB normalization records that state as delivered
when only notification publishing failed. This launcher mirrors that boundary
in the scheduler: a notification failure does not turn a successful agent turn
into fallback scheduling.
"""

from __future__ import annotations

from typing import Any

import autonomous_wake_worker as worker


_original_bridge = worker._bridge


async def _notification_compatible_bridge(client, config, body: dict[str, Any]):
    try:
        return await _original_bridge(client, config, body)
    except RuntimeError as error:
        if str(error) == "notification_failed" and body.get("op") == "deliver":
            worker._safe_log("notification_failed")
            return {
                "ok": True,
                "status": "delivered",
                "notification_failed": True,
            }
        raise


worker._bridge = _notification_compatible_bridge


if __name__ == "__main__":
    raise SystemExit(worker.main())
