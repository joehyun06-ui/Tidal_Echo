"""Bounded, data-free production observability for Codex generation jobs.

This module intentionally exposes no chat text, session id, generation id, thread id,
model, account identity, callback identity, input digest, or workspace path.  It is
safe to call during startup and must never make the application unavailable merely
because the diagnostic read fails.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import codex_generation_store as store


_MAX_COUNT = 1_000_000
_MAX_TIMESTAMP = 64


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise ValueError("codex_generation_observability_invalid")
    return value


def _safe_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TIMESTAMP or not value.isascii():
        raise ValueError("codex_generation_observability_invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("codex_generation_observability_invalid") from None
    return value


def latest_job_snapshot(store_path: str | Path) -> dict[str, object]:
    """Return only bounded lifecycle metadata for the newest durable Codex job."""
    try:
        with closing(store.connect(store_path)) as conn:
            row = conn.execute(
                """SELECT status,attempt_count,recovery_count,turn_id,
                          assistant_message_id,created_at,updated_at
                   FROM codex_generation_jobs ORDER BY id DESC LIMIT 1"""
            ).fetchone()
    except Exception:
        return {"state": "unavailable"}

    if row is None:
        return {"state": "empty"}

    try:
        status = row["status"]
        if status not in store.JOB_STATUSES:
            raise ValueError("codex_generation_observability_invalid")
        return {
            "state": "present",
            "status": str(status),
            "attempt_count": _safe_count(row["attempt_count"]),
            "recovery_count": _safe_count(row["recovery_count"]),
            "turn_bound": row["turn_id"] is not None,
            "assistant_message_bound": row["assistant_message_id"] is not None,
            "created_at": _safe_timestamp(row["created_at"]),
            "updated_at": _safe_timestamp(row["updated_at"]),
        }
    except Exception:
        return {"state": "unavailable"}


def format_latest_job_snapshot(snapshot: Mapping[str, object]) -> str:
    state = snapshot.get("state")
    if state == "empty":
        return "[codex-generation] latest_job=empty"
    if state != "present":
        return "[codex-generation] latest_job=unavailable"
    return (
        "[codex-generation] latest_job=present "
        f"status={snapshot['status']} "
        f"attempt_count={snapshot['attempt_count']} "
        f"recovery_count={snapshot['recovery_count']} "
        f"turn_bound={'true' if snapshot['turn_bound'] else 'false'} "
        f"assistant_message_bound={'true' if snapshot['assistant_message_bound'] else 'false'} "
        f"created_at={snapshot['created_at']} "
        f"updated_at={snapshot['updated_at']}"
    )


def log_latest_job_snapshot(
    store_path: str | Path,
    *,
    stream: TextIO | None = None,
) -> None:
    target = sys.stderr if stream is None else stream
    print(
        format_latest_job_snapshot(latest_job_snapshot(store_path)),
        file=target,
        flush=True,
    )
