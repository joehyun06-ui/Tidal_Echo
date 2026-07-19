"""Deterministic, local-only Dylan heartbeat foundation.

This module never calls a model, Telegram, ntfy, or any other network service.
It performs one bounded database transaction per requested tick.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from . import channel_store, deployment_config


DECISIONS = frozenset({
    "disabled", "quiet_hours", "cooldown", "observe", "journal_candidate", "contact_candidate",
})
CANDIDATE_DECISIONS = frozenset({"observe", "journal_candidate", "contact_candidate"})
TIMELINE_SUMMARIES = {
    "disabled": "heartbeat disabled",
    "quiet_hours": "quiet hours active",
    "cooldown": "cooldown active",
    "observe": "heartbeat observed",
    "journal_candidate": "heartbeat observed",
    "contact_candidate": "contact candidate deferred",
}
JOURNAL_CONTENT = {
    "journal_candidate": "heartbeat observed",
    "contact_candidate": "contact candidate deferred",
}


@dataclass(frozen=True)
class HeartbeatRunResult:
    run_id: str
    scheduled_at: str
    outcome: str
    decision: str | None
    duplicate: bool
    recovered: bool
    journal_entry_id: int | None = None
    timeline_event_id: int | None = None
    error_category: str | None = None


_LOCKS_GUARD = threading.Lock()


@dataclass
class _LockEntry:
    lock: threading.Lock
    users: int = 0


_PATH_LOCKS: dict[str, _LockEntry] = {}


@contextmanager
def _heartbeat_lock(path: str):
    key = str(Path(path).resolve(strict=False))
    with _LOCKS_GUARD:
        entry = _PATH_LOCKS.get(key)
        if entry is None:
            entry = _LockEntry(threading.Lock())
            _PATH_LOCKS[key] = entry
        entry.users += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _PATH_LOCKS.get(key) is entry:
                del _PATH_LOCKS[key]


def _utc(value: datetime, category: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(category)
    return value.astimezone(timezone.utc)


def _parse_persisted_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("heartbeat_state_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("heartbeat_state_invalid")
    return parsed.astimezone(timezone.utc)


def _is_quiet(local_clock: time, start: time, end: time) -> bool:
    if start < end:
        return start <= local_clock < end
    return local_clock >= start or local_clock < end


def decide_heartbeat(
    config: deployment_config.HeartbeatConfig,
    at_utc: datetime,
    last_contact_at: datetime | None,
    candidate_decision: str = "observe",
) -> str:
    """Return one deterministic foundation decision for already-validated inputs."""
    if candidate_decision not in CANDIDATE_DECISIONS:
        raise ValueError("invalid_heartbeat_candidate_decision")
    instant = _utc(at_utc, "invalid_heartbeat_time")
    if not config.enabled:
        return "disabled"
    local_clock = instant.astimezone(ZoneInfo(config.timezone)).timetz().replace(tzinfo=None)
    if _is_quiet(local_clock, config.quiet_hours_start, config.quiet_hours_end):
        return "quiet_hours"
    if last_contact_at is not None:
        contact = _utc(last_contact_at, "heartbeat_state_invalid")
        if (instant - contact).total_seconds() < config.contact_cooldown_seconds:
            return "cooldown"
    return candidate_decision


def _tick_identity(scheduled_at: datetime, interval_seconds: int) -> tuple[str, str, str]:
    bucket_epoch = int(scheduled_at.timestamp()) // interval_seconds * interval_seconds
    bucket = datetime.fromtimestamp(bucket_epoch, timezone.utc).isoformat()
    raw = f"dylan-heartbeat-v1\x1f{interval_seconds}\x1f{bucket}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return bucket, digest, "heartbeat-" + digest[:32]


def _record_key(run_id: str, kind: str) -> str:
    return hashlib.sha256(f"{run_id}\x1f{kind}".encode("utf-8")).hexdigest()


def _ensure_state(conn, stamp: str):
    conn.execute(
        """INSERT OR IGNORE INTO heartbeat_state
           (state_key,consecutive_failures,status,created_at,updated_at)
           VALUES('default',0,'idle',?,?)""",
        (stamp, stamp),
    )
    row = conn.execute("SELECT * FROM heartbeat_state WHERE state_key='default'").fetchone()
    if row is None:
        raise ValueError("heartbeat_state_invalid")
    return row


def _existing_result(conn, row) -> HeartbeatRunResult:
    journal = conn.execute(
        "SELECT id FROM journal_entries WHERE heartbeat_run_id=? ORDER BY id LIMIT 1", (row["id"],)
    ).fetchone()
    timeline = conn.execute(
        "SELECT id FROM timeline_events WHERE heartbeat_run_id=? ORDER BY id LIMIT 1", (row["id"],)
    ).fetchone()
    return HeartbeatRunResult(
        row["run_id"], row["scheduled_at"], row["outcome"], row["decision"], True, False,
        journal["id"] if journal else None, timeline["id"] if timeline else None,
        row["error_category"],
    )


def run_heartbeat_once(
    path: str,
    config: deployment_config.HeartbeatConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    scheduled_at: datetime | None = None,
    now: datetime | None = None,
    candidate_decision: str = "observe",
) -> HeartbeatRunResult:
    """Run one local-only, transactionally deduplicated heartbeat tick."""
    resolved_config = config or deployment_config.load_heartbeat_config(environ)
    scheduled = _utc(scheduled_at or datetime.now(timezone.utc), "invalid_heartbeat_scheduled_at")
    started = _utc(now or datetime.now(timezone.utc), "invalid_heartbeat_started_at")
    bucket, dedupe_key, run_id = _tick_identity(scheduled, resolved_config.interval_seconds)
    started_stamp = started.isoformat()
    metadata = json.dumps({
        "foundation_version": 1,
        "candidate_decision": candidate_decision,
        "timezone": resolved_config.timezone,
    }, sort_keys=True, separators=(",", ":"))

    with _heartbeat_lock(path):
        channel_store.run_migrations(path)
        with channel_store.connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM heartbeat_runs WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
                if existing is not None and existing["outcome"] == "completed":
                    result = _existing_result(conn, existing)
                    conn.execute("COMMIT")
                    return result
                state = _ensure_state(conn, started_stamp)
                recovered = existing is not None
                if existing is None:
                    cursor = conn.execute(
                        """INSERT INTO heartbeat_runs
                           (run_id,dedupe_key,scheduled_at,started_at,outcome,metadata_json,
                            attempt_count,created_at,updated_at)
                           VALUES(?,?,?,?,'running',?,1,?,?)""",
                        (run_id, dedupe_key, bucket, started_stamp, metadata, started_stamp, started_stamp),
                    )
                    run_row_id = int(cursor.lastrowid)
                else:
                    run_row_id = int(existing["id"])
                    conn.execute(
                        """UPDATE heartbeat_runs SET started_at=?,completed_at=NULL,outcome='running',
                           decision=NULL,error_category=NULL,metadata_json=?,attempt_count=attempt_count+1,
                           updated_at=? WHERE id=?""",
                        (started_stamp, metadata, started_stamp, run_row_id),
                    )

                conn.execute("SAVEPOINT heartbeat_execution")
                try:
                    last_contact = _parse_persisted_utc(state["last_contact_at"])
                    decision = decide_heartbeat(
                        resolved_config, scheduled, last_contact, candidate_decision
                    )
                    timeline = conn.execute(
                        """INSERT INTO timeline_events
                           (event_type,summary,event_at,source,heartbeat_run_id,dedupe_key)
                           VALUES(?,?,?,'heartbeat',?,?)""",
                        (decision, TIMELINE_SUMMARIES[decision], bucket, run_row_id,
                         _record_key(run_id, "timeline")),
                    )
                    journal_id = None
                    if decision in JOURNAL_CONTENT:
                        journal = conn.execute(
                            """INSERT INTO journal_entries
                               (entry_type,content,created_at,source,heartbeat_run_id,dedupe_key)
                               VALUES(?,?,?,'heartbeat',?,?)""",
                            (decision, JOURNAL_CONTENT[decision], started_stamp, run_row_id,
                             _record_key(run_id, "journal")),
                        )
                        journal_id = int(journal.lastrowid)
                    completed_stamp = started_stamp
                    pause_reason = decision if decision in {"disabled", "quiet_hours", "cooldown"} else None
                    conn.execute(
                        """UPDATE heartbeat_state SET last_tick_at=?,last_success_at=?,
                           consecutive_failures=0,status=?,pause_reason=?,updated_at=?
                           WHERE state_key='default'""",
                        (bucket, completed_stamp, decision, pause_reason, completed_stamp),
                    )
                    changed = conn.execute(
                        """UPDATE heartbeat_runs SET completed_at=?,outcome='completed',decision=?,
                           error_category=NULL,updated_at=? WHERE id=? AND outcome='running'""",
                        (completed_stamp, decision, completed_stamp, run_row_id),
                    ).rowcount
                    if changed != 1:
                        raise ValueError("heartbeat_run_state_changed")
                    conn.execute("RELEASE SAVEPOINT heartbeat_execution")
                    conn.execute("COMMIT")
                    return HeartbeatRunResult(
                        run_id, bucket, "completed", decision, False, recovered,
                        journal_id, int(timeline.lastrowid), None,
                    )
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT heartbeat_execution")
                    conn.execute("RELEASE SAVEPOINT heartbeat_execution")
                    error_category = "heartbeat_execution_failed"
                    conn.execute(
                        """UPDATE heartbeat_runs SET completed_at=?,outcome='failed',decision=NULL,
                           error_category=?,updated_at=? WHERE id=?""",
                        (started_stamp, error_category, started_stamp, run_row_id),
                    )
                    conn.execute(
                        """UPDATE heartbeat_state SET last_tick_at=?,
                           consecutive_failures=consecutive_failures+1,status='failed',pause_reason=?,
                           updated_at=? WHERE state_key='default'""",
                        (bucket, error_category, started_stamp),
                    )
                    conn.execute("COMMIT")
                    return HeartbeatRunResult(
                        run_id, bucket, "failed", None, False, recovered,
                        error_category=error_category,
                    )
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
