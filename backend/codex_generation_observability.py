"""Bounded, data-free production observability for Codex generation jobs.

This module intentionally exposes no chat text, session id, generation id, thread id,
model, account identity, callback identity, input digest, or workspace path.  It is
safe to call during startup and must never make the application unavailable merely
because a diagnostic read fails.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import codex_generation_store as store


_MAX_COUNT = 1_000_000
_MAX_TIMESTAMP = 64
_MAX_RECEIPT_ROWS = 16


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise ValueError("codex_generation_observability_invalid")
    return value


def _safe_message_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("codex_generation_observability_invalid")
    return value


def _safe_private_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
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


def _read_only_connection(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_absolute() or not database.is_file():
        raise ValueError("codex_generation_observability_unavailable")
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


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


def recent_ingress_receipt(
    relay_db: str | Path,
    store_path: str | Path,
    *,
    limit: int = 8,
) -> dict[str, object]:
    """Correlate recent canonical inbound ids with durable Codex job existence.

    The receipt deliberately omits message text and session metadata.  Canonical ids
    and timestamps are sufficient to correlate a bounded production test with relay
    logs while proving whether that exact canonical input entered Codex generation.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RECEIPT_ROWS:
        return {"state": "unavailable", "rows": []}
    try:
        with closing(_read_only_connection(relay_db)) as relay_conn:
            rows = relay_conn.execute(
                """SELECT id,ts FROM messages
                   WHERE direction='in' AND kind IN ('user','voice')
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        with closing(store.connect(store_path)) as codex_conn:
            projected = []
            for row in reversed(rows):
                message_id = _safe_message_id(row["id"])
                timestamp = _safe_timestamp(row["ts"])
                job = codex_conn.execute(
                    """SELECT status FROM codex_generation_jobs
                       WHERE canonical_message_id=?""",
                    (message_id,),
                ).fetchone()
                if job is None:
                    projected.append({
                        "canonical_message_id": message_id,
                        "ts": timestamp,
                        "codex_job": False,
                    })
                    continue
                status = job["status"]
                if status not in store.JOB_STATUSES:
                    raise ValueError("codex_generation_observability_invalid")
                projected.append({
                    "canonical_message_id": message_id,
                    "ts": timestamp,
                    "codex_job": True,
                    "codex_status": str(status),
                })
    except Exception:
        return {"state": "unavailable", "rows": []}
    return {"state": "present", "rows": projected}


def recent_codex_continuity_receipt(
    store_path: str | Path,
    *,
    limit: int = 3,
) -> dict[str, object]:
    """Project whether recent Codex jobs share one durable session/thread.

    Actual session/thread identifiers are used only for equality checks and are never
    returned.  Canonical message ids are retained solely for correlation with the
    already bounded provider receipt.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 2 <= limit <= _MAX_RECEIPT_ROWS:
        return {"state": "unavailable"}
    try:
        with closing(store.connect(store_path)) as conn:
            rows = conn.execute(
                """SELECT canonical_message_id,api_session,thread_attempt_id,thread_id,
                          status,created_at
                   FROM codex_generation_jobs ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            rows = list(reversed(rows))
            if not rows:
                return {"state": "empty", "job_count": 0}
            if len(rows) < 2:
                return {"state": "insufficient", "job_count": len(rows)}

            canonical_ids: list[int] = []
            sessions: list[str] = []
            thread_ids: list[str | None] = []
            thread_attempt_ids: list[str | None] = []
            for row in rows:
                canonical_ids.append(_safe_message_id(row["canonical_message_id"]))
                sessions.append(_safe_private_id(row["api_session"]))
                status = row["status"]
                if status not in store.JOB_STATUSES:
                    raise ValueError("codex_generation_observability_invalid")
                _safe_timestamp(row["created_at"])
                thread_ids.append(
                    None if row["thread_id"] is None else _safe_private_id(row["thread_id"])
                )
                thread_attempt_ids.append(
                    None
                    if row["thread_attempt_id"] is None
                    else _safe_private_id(row["thread_attempt_id"])
                )

            same_session = len(set(sessions)) == 1
            all_thread_bound = all(value is not None for value in thread_ids)
            same_thread = all_thread_bound and len(set(thread_ids)) == 1
            all_thread_attempt_bound = all(value is not None for value in thread_attempt_ids)
            same_thread_attempt = (
                all_thread_attempt_bound and len(set(thread_attempt_ids)) == 1
            )
            session_active = False
            current_thread_matches = False
            if same_session:
                session = conn.execute(
                    """SELECT status,thread_attempt_id,thread_id
                       FROM codex_sessions WHERE api_session=?""",
                    (sessions[-1],),
                ).fetchone()
                if session is None or session["status"] not in {"active", "retired"}:
                    raise ValueError("codex_generation_observability_invalid")
                session_active = session["status"] == "active"
                session_thread_id = (
                    None
                    if session["thread_id"] is None
                    else _safe_private_id(session["thread_id"])
                )
                session_attempt_id = (
                    None
                    if session["thread_attempt_id"] is None
                    else _safe_private_id(session["thread_attempt_id"])
                )
                current_thread_matches = bool(
                    all_thread_bound
                    and all_thread_attempt_bound
                    and session_thread_id == thread_ids[-1]
                    and session_attempt_id == thread_attempt_ids[-1]
                )
    except Exception:
        return {"state": "unavailable"}

    return {
        "state": "present",
        "job_count": len(rows),
        "canonical_message_ids": canonical_ids,
        "same_session": same_session,
        "all_thread_bound": all_thread_bound,
        "same_thread": same_thread,
        "all_thread_attempt_bound": all_thread_attempt_bound,
        "same_thread_attempt": same_thread_attempt,
        "session_active": session_active,
        "current_thread_matches": current_thread_matches,
    }


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


def format_ingress_receipt(receipt: Mapping[str, object]) -> list[str]:
    if receipt.get("state") != "present" or not isinstance(receipt.get("rows"), list):
        return ["[provider-receipt] state=unavailable"]
    rows = receipt["rows"]
    if not rows:
        return ["[provider-receipt] state=empty"]
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            return ["[provider-receipt] state=unavailable"]
        message_id = _safe_message_id(row.get("canonical_message_id"))
        timestamp = _safe_timestamp(row.get("ts"))
        codex_job = row.get("codex_job")
        if not isinstance(codex_job, bool):
            return ["[provider-receipt] state=unavailable"]
        line = (
            "[provider-receipt] "
            f"canonical_message_id={message_id} ts={timestamp} "
            f"codex_job={'true' if codex_job else 'false'}"
        )
        if codex_job:
            status = row.get("codex_status")
            if status not in store.JOB_STATUSES:
                return ["[provider-receipt] state=unavailable"]
            line += f" codex_status={status}"
        lines.append(line)
    return lines


def format_codex_continuity_receipt(receipt: Mapping[str, object]) -> str:
    state = receipt.get("state")
    if state == "empty":
        return "[codex-continuity] state=empty jobs=0"
    if state == "insufficient":
        count = _safe_count(receipt.get("job_count"))
        return f"[codex-continuity] state=insufficient jobs={count}"
    if state != "present":
        return "[codex-continuity] state=unavailable"

    count = _safe_count(receipt.get("job_count"))
    canonical_ids = receipt.get("canonical_message_ids")
    if (
        not isinstance(canonical_ids, list)
        or len(canonical_ids) != count
        or not 2 <= count <= _MAX_RECEIPT_ROWS
    ):
        return "[codex-continuity] state=unavailable"
    ids = ",".join(str(_safe_message_id(value)) for value in canonical_ids)
    bool_fields = (
        "same_session",
        "all_thread_bound",
        "same_thread",
        "all_thread_attempt_bound",
        "same_thread_attempt",
        "session_active",
        "current_thread_matches",
    )
    values: dict[str, bool] = {}
    for field in bool_fields:
        value = receipt.get(field)
        if not isinstance(value, bool):
            return "[codex-continuity] state=unavailable"
        values[field] = value
    return (
        "[codex-continuity] state=present "
        f"jobs={count} canonical_message_ids={ids} "
        f"same_session={'true' if values['same_session'] else 'false'} "
        f"all_thread_bound={'true' if values['all_thread_bound'] else 'false'} "
        f"same_thread={'true' if values['same_thread'] else 'false'} "
        f"all_thread_attempt_bound={'true' if values['all_thread_attempt_bound'] else 'false'} "
        f"same_thread_attempt={'true' if values['same_thread_attempt'] else 'false'} "
        f"session_active={'true' if values['session_active'] else 'false'} "
        f"current_thread_matches={'true' if values['current_thread_matches'] else 'false'}"
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


def log_recent_ingress_receipt(
    relay_db: str | Path,
    store_path: str | Path,
    *,
    limit: int = 8,
    stream: TextIO | None = None,
) -> None:
    target = sys.stderr if stream is None else stream
    for line in format_ingress_receipt(
        recent_ingress_receipt(relay_db, store_path, limit=limit)
    ):
        print(line, file=target, flush=True)
    print(
        format_codex_continuity_receipt(
            recent_codex_continuity_receipt(store_path, limit=3)
        ),
        file=target,
        flush=True,
    )
