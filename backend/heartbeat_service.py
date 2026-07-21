"""Deterministic, local-only Dylan heartbeat foundation.

This module never calls a model, Telegram, ntfy, or any other network service.
It performs one bounded database transaction per requested tick.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import channel_store, deployment_config


DECISIONS = frozenset({
    "disabled", "quiet_hours", "cooldown", "observe", "journal_candidate", "contact_candidate",
})
CANDIDATE_DECISIONS = DECISIONS
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
SAFE_METADATA_JSON = json.dumps(
    {"foundation_version": 2}, sort_keys=True, separators=(",", ":"),
)
_SCHEDULE_REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
    database_error_category: str | None = None


@dataclass
class _LockEntry:
    lock: threading.Lock
    users: int = 0


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, _LockEntry] = {}


@contextmanager
def _heartbeat_lock(canonical_path: str):
    with _LOCKS_GUARD:
        entry = _PATH_LOCKS.get(canonical_path)
        if entry is None:
            entry = _LockEntry(threading.Lock())
            _PATH_LOCKS[canonical_path] = entry
        entry.users += 1
    acquired = False
    try:
        entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _PATH_LOCKS.get(canonical_path) is entry:
                del _PATH_LOCKS[canonical_path]


def _canonical_database_path(path: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError
        resolved = str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        raise ValueError("invalid_heartbeat_database_path") from None
    return os.path.normcase(resolved)


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


def _validate_heartbeat_config(config: deployment_config.HeartbeatConfig) -> None:
    if not isinstance(config, deployment_config.HeartbeatConfig):
        raise ValueError("invalid_heartbeat_config")
    if type(config.enabled) is not bool:
        raise ValueError("invalid_heartbeat_config")
    if type(config.interval_seconds) is not int or not 30 <= config.interval_seconds <= 86400:
        raise ValueError("invalid_heartbeat_config")
    if (
        not isinstance(config.timezone, str) or not config.timezone
        or config.timezone != config.timezone.strip() or not config.timezone.isascii()
        or len(config.timezone) > 128
    ):
        raise ValueError("invalid_heartbeat_config")
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("invalid_heartbeat_config") from None
    for boundary in (config.quiet_hours_start, config.quiet_hours_end):
        if (
            not isinstance(boundary, time) or boundary.tzinfo is not None
            or boundary.second != 0 or boundary.microsecond != 0
        ):
            raise ValueError("invalid_heartbeat_config")
    if config.quiet_hours_start == config.quiet_hours_end:
        raise ValueError("invalid_heartbeat_config")
    if (
        type(config.contact_cooldown_seconds) is not int
        or not 0 <= config.contact_cooldown_seconds <= 2592000
    ):
        raise ValueError("invalid_heartbeat_config")
    if (
        not isinstance(config.schedule_revision, str)
        or not config.schedule_revision.isascii()
        or _SCHEDULE_REVISION_PATTERN.fullmatch(config.schedule_revision) is None
    ):
        raise ValueError("invalid_heartbeat_config")


def _validate_candidate_decision(candidate_decision: object) -> str:
    if not isinstance(candidate_decision, str) or candidate_decision not in CANDIDATE_DECISIONS:
        raise ValueError("invalid_heartbeat_candidate_decision")
    return candidate_decision


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
    """Return one deterministic foundation decision for validated inputs."""
    _validate_heartbeat_config(config)
    candidate = _validate_candidate_decision(candidate_decision)
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
    return candidate


def _hash_object(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schedule_fingerprint(config: deployment_config.HeartbeatConfig) -> str:
    return _hash_object({
        "foundation_version": 2,
        "interval_seconds": config.interval_seconds,
        "timezone": config.timezone,
        "quiet_hours_start": config.quiet_hours_start.strftime("%H:%M"),
        "quiet_hours_end": config.quiet_hours_end.strftime("%H:%M"),
        "contact_cooldown_seconds": config.contact_cooldown_seconds,
    })


def _input_fingerprint(
    config: deployment_config.HeartbeatConfig, candidate_decision: str,
    schedule_fingerprint: str,
) -> str:
    return _hash_object({
        "foundation_version": 2,
        "schedule_revision": config.schedule_revision,
        "schedule_fingerprint": schedule_fingerprint,
        "enabled": config.enabled,
        "candidate_decision": candidate_decision,
    })


def _tick_identity(
    scheduled_at: datetime, interval_seconds: int, schedule_revision: str,
) -> tuple[str, str, str]:
    elapsed = scheduled_at - _EPOCH
    total_microseconds = (
        (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    )
    interval_microseconds = interval_seconds * 1_000_000
    bucket_microseconds = total_microseconds // interval_microseconds * interval_microseconds
    bucket = (_EPOCH + timedelta(microseconds=bucket_microseconds)).isoformat()
    raw = f"dylan-heartbeat-v2\x1f{schedule_revision}\x1f{bucket}".encode("utf-8")
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


def _select_run(conn, dedupe_key: str):
    return conn.execute(
        """SELECT r.*,i.schedule_revision AS input_schedule_revision,i.input_fingerprint
           FROM heartbeat_runs r
           LEFT JOIN heartbeat_run_inputs i ON i.heartbeat_run_id=r.id
           WHERE r.dedupe_key=?""",
        (dedupe_key,),
    ).fetchone()


def _existing_result(conn, row, *, duplicate: bool = True) -> HeartbeatRunResult:
    journal = conn.execute(
        "SELECT id FROM journal_entries WHERE heartbeat_run_id=? ORDER BY id LIMIT 1", (row["id"],)
    ).fetchone()
    timeline = conn.execute(
        "SELECT id FROM timeline_events WHERE heartbeat_run_id=? ORDER BY id LIMIT 1", (row["id"],)
    ).fetchone()
    return HeartbeatRunResult(
        run_id=row["run_id"], scheduled_at=row["scheduled_at"], outcome=row["outcome"],
        decision=row["decision"], duplicate=duplicate, recovered=False,
        journal_entry_id=journal["id"] if journal else None,
        timeline_event_id=timeline["id"] if timeline else None,
        error_category=row["error_category"],
    )


def _database_error_category(error: BaseException) -> str | None:
    if not isinstance(error, sqlite3.Error):
        return None
    name = str(getattr(error, "sqlite_errorname", "") or "").upper()
    for prefix, category in (
        ("SQLITE_BUSY", "sqlite_busy"),
        ("SQLITE_LOCKED", "sqlite_locked"),
        ("SQLITE_FULL", "sqlite_full"),
        ("SQLITE_IOERR", "sqlite_io_error"),
        ("SQLITE_READONLY", "sqlite_readonly"),
        ("SQLITE_CONSTRAINT", "sqlite_constraint"),
        ("SQLITE_CORRUPT", "sqlite_corrupt"),
        ("SQLITE_NOTADB", "sqlite_not_database"),
    ):
        if name.startswith(prefix):
            return category
    return "sqlite_error"


def _safe_rollback(conn) -> bool:
    if not conn.in_transaction:
        return True
    try:
        conn.execute("ROLLBACK")
    except Exception:
        return False
    return True


@contextmanager
def _explicit_transaction_connection(path: str):
    """Close without sqlite3's context-manager implicit commit behavior."""
    conn = channel_store.connect(path)
    try:
        yield conn
    finally:
        if conn.in_transaction:
            _safe_rollback(conn)
        conn.close()


def _failure_result(
    run_id: str, bucket: str, category: str, *, recovered: bool,
    database_error_category: str | None = None,
) -> HeartbeatRunResult:
    outcome = "uncertain" if category == "commit_uncertain" else "failed"
    return HeartbeatRunResult(
        run_id=run_id, scheduled_at=bucket, outcome=outcome, decision=None,
        duplicate=False, recovered=recovered, error_category=category,
        database_error_category=database_error_category,
    )


def _reconcile_commit(
    path: str, dedupe_key: str, input_fingerprint: str, expected: HeartbeatRunResult,
    database_error_category: str | None,
) -> HeartbeatRunResult:
    try:
        with _explicit_transaction_connection(path) as verify:
            row = _select_run(verify, dedupe_key)
            if row is None:
                return replace(
                    expected, outcome="failed", decision=None, journal_entry_id=None,
                    timeline_event_id=None, error_category="commit_failed",
                    database_error_category=database_error_category,
                )
            if (
                row["input_fingerprint"] == input_fingerprint
                and row["outcome"] == expected.outcome
                and row["decision"] == expected.decision
            ):
                return expected
    except Exception:
        pass
    return replace(
        expected, outcome="uncertain", decision=None, journal_entry_id=None,
        timeline_event_id=None, error_category="commit_uncertain",
        database_error_category=database_error_category,
    )


def _commit_transaction(
    conn, path: str, dedupe_key: str, input_fingerprint: str, result: HeartbeatRunResult,
) -> HeartbeatRunResult:
    try:
        conn.execute("COMMIT")
        return result
    except Exception as error:
        database_category = _database_error_category(error)
        if conn.in_transaction:
            if _safe_rollback(conn):
                return replace(
                    result, outcome="failed", decision=None, journal_entry_id=None,
                    timeline_event_id=None, error_category="commit_failed",
                    database_error_category=database_category,
                )
            return replace(
                result, outcome="uncertain", decision=None, journal_entry_id=None,
                timeline_event_id=None, error_category="commit_uncertain",
                database_error_category=database_category,
            )
        return _reconcile_commit(
            path, dedupe_key, input_fingerprint, result, database_category,
        )


def _reject_result(run_id: str, bucket: str, category: str, *, duplicate: bool = False):
    return HeartbeatRunResult(
        run_id=run_id, scheduled_at=bucket, outcome="conflict", decision=None,
        duplicate=duplicate, recovered=False, error_category=category,
    )


def run_heartbeat_once(
    path: str | os.PathLike[str],
    config: deployment_config.HeartbeatConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    scheduled_at: datetime | None = None,
    now: datetime | None = None,
    candidate_decision: str = "observe",
) -> HeartbeatRunResult:
    """Run one local-only, transactionally deduplicated heartbeat tick.

    A logical identity is the stable schedule revision plus its UTC interval
    bucket. Candidate decisions are excluded from identity but included in the
    input fingerprint, so changed inputs conflict instead of reusing a result.
    """
    resolved_config = config if config is not None else deployment_config.load_heartbeat_config(environ)
    _validate_heartbeat_config(resolved_config)
    candidate = _validate_candidate_decision(candidate_decision)
    scheduled = _utc(scheduled_at or datetime.now(timezone.utc), "invalid_heartbeat_scheduled_at")
    started = _utc(now or datetime.now(timezone.utc), "invalid_heartbeat_started_at")
    canonical_path = _canonical_database_path(path)
    schedule_fingerprint = _schedule_fingerprint(resolved_config)
    input_fingerprint = _input_fingerprint(resolved_config, candidate, schedule_fingerprint)
    bucket, dedupe_key, run_id = _tick_identity(
        scheduled, resolved_config.interval_seconds, resolved_config.schedule_revision,
    )
    started_stamp = started.isoformat()

    with _heartbeat_lock(canonical_path):
        channel_store.run_migrations(canonical_path)
        with _explicit_transaction_connection(canonical_path) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception as error:
                return _failure_result(
                    run_id, bucket, "heartbeat_execution_failed", recovered=False,
                    database_error_category=_database_error_category(error),
                )
            recovered = False
            try:
                schedule_row = conn.execute(
                    """SELECT schedule_fingerprint FROM heartbeat_schedule_revisions
                       WHERE schedule_revision=?""",
                    (resolved_config.schedule_revision,),
                ).fetchone()
                if (
                    schedule_row is not None
                    and schedule_row["schedule_fingerprint"] != schedule_fingerprint
                ):
                    conn.execute("ROLLBACK")
                    return _reject_result(run_id, bucket, "schedule_revision_conflict")

                existing = _select_run(conn, dedupe_key)
                if existing is not None:
                    if (
                        existing["input_schedule_revision"] != resolved_config.schedule_revision
                        or existing["input_fingerprint"] != input_fingerprint
                    ):
                        conn.execute("ROLLBACK")
                        return _reject_result(
                            existing["run_id"], existing["scheduled_at"],
                            "input_fingerprint_conflict", duplicate=True,
                        )
                    if existing["outcome"] == "completed":
                        result = _existing_result(conn, existing)
                        conn.execute("ROLLBACK")
                        return result

                state = _ensure_state(conn, started_stamp)
                last_tick = _parse_persisted_utc(state["last_tick_at"])
                bucket_time = _parse_persisted_utc(bucket)
                if last_tick is not None and bucket_time is not None:
                    if bucket_time < last_tick:
                        conn.execute("ROLLBACK")
                        return _reject_result(run_id, bucket, "stale_clock")
                    if bucket_time == last_tick and existing is None:
                        conn.execute("ROLLBACK")
                        return _reject_result(run_id, bucket, "logical_tick_conflict")

                recovered = existing is not None
                if schedule_row is None:
                    conn.execute(
                        """INSERT INTO heartbeat_schedule_revisions
                           (schedule_revision,schedule_fingerprint,created_at,updated_at)
                           VALUES(?,?,?,?)""",
                        (
                            resolved_config.schedule_revision, schedule_fingerprint,
                            started_stamp, started_stamp,
                        ),
                    )
                if existing is None:
                    cursor = conn.execute(
                        """INSERT INTO heartbeat_runs
                           (run_id,dedupe_key,scheduled_at,started_at,outcome,metadata_json,
                            attempt_count,created_at,updated_at)
                           VALUES(?,?,?,?,'running',?,1,?,?)""",
                        (
                            run_id, dedupe_key, bucket, started_stamp, SAFE_METADATA_JSON,
                            started_stamp, started_stamp,
                        ),
                    )
                    run_row_id = int(cursor.lastrowid)
                    conn.execute(
                        """INSERT INTO heartbeat_run_inputs
                           (heartbeat_run_id,schedule_revision,input_fingerprint,created_at)
                           VALUES(?,?,?,?)""",
                        (
                            run_row_id, resolved_config.schedule_revision,
                            input_fingerprint, started_stamp,
                        ),
                    )
                else:
                    run_row_id = int(existing["id"])
                    conn.execute(
                        """UPDATE heartbeat_runs SET started_at=?,completed_at=NULL,outcome='running',
                           decision=NULL,error_category=NULL,metadata_json=?,attempt_count=attempt_count+1,
                           updated_at=? WHERE id=?""",
                        (started_stamp, SAFE_METADATA_JSON, started_stamp, run_row_id),
                    )

                conn.execute("SAVEPOINT heartbeat_execution")
                savepoint_active = True
                try:
                    last_contact = _parse_persisted_utc(state["last_contact_at"])
                    decision = decide_heartbeat(
                        resolved_config, scheduled, last_contact, candidate,
                    )
                    timeline = conn.execute(
                        """INSERT INTO timeline_events
                           (event_type,summary,event_at,source,heartbeat_run_id,dedupe_key)
                           VALUES(?,?,?,'heartbeat',?,?)""",
                        (
                            decision, TIMELINE_SUMMARIES[decision], bucket, run_row_id,
                            _record_key(run_id, "timeline"),
                        ),
                    )
                    journal_id = None
                    if decision in JOURNAL_CONTENT:
                        journal = conn.execute(
                            """INSERT INTO journal_entries
                               (entry_type,content,created_at,source,heartbeat_run_id,dedupe_key)
                               VALUES(?,?,?,'heartbeat',?,?)""",
                            (
                                decision, JOURNAL_CONTENT[decision], started_stamp, run_row_id,
                                _record_key(run_id, "journal"),
                            ),
                        )
                        journal_id = int(journal.lastrowid)
                    pause_reason = (
                        decision if decision in {"disabled", "quiet_hours", "cooldown"} else None
                    )
                    conn.execute(
                        """UPDATE heartbeat_state SET last_tick_at=?,last_success_at=?,
                           consecutive_failures=0,status=?,pause_reason=?,updated_at=?
                           WHERE state_key='default'""",
                        (bucket, started_stamp, decision, pause_reason, started_stamp),
                    )
                    changed = conn.execute(
                        """UPDATE heartbeat_runs SET completed_at=?,outcome='completed',decision=?,
                           error_category=NULL,updated_at=? WHERE id=? AND outcome='running'""",
                        (started_stamp, decision, started_stamp, run_row_id),
                    ).rowcount
                    if changed != 1:
                        raise ValueError("heartbeat_run_state_changed")
                    conn.execute("RELEASE SAVEPOINT heartbeat_execution")
                    savepoint_active = False
                    result = HeartbeatRunResult(
                        run_id=run_id, scheduled_at=bucket, outcome="completed", decision=decision,
                        duplicate=False, recovered=recovered, journal_entry_id=journal_id,
                        timeline_event_id=int(timeline.lastrowid),
                    )
                except Exception as execution_error:
                    database_category = _database_error_category(execution_error)
                    try:
                        if savepoint_active:
                            conn.execute("ROLLBACK TO SAVEPOINT heartbeat_execution")
                            conn.execute("RELEASE SAVEPOINT heartbeat_execution")
                            savepoint_active = False
                    except Exception as rollback_error:
                        _safe_rollback(conn)
                        return _failure_result(
                            run_id, bucket, "heartbeat_execution_failed", recovered=recovered,
                            database_error_category=(
                                database_category or _database_error_category(rollback_error)
                            ),
                        )
                    try:
                        persisted_error_category = (
                            database_category or "heartbeat_execution_failed"
                        )
                        conn.execute(
                            """UPDATE heartbeat_runs SET completed_at=?,outcome='failed',decision=NULL,
                               error_category=?,updated_at=? WHERE id=?""",
                            (
                                started_stamp, persisted_error_category,
                                started_stamp, run_row_id,
                            ),
                        )
                        conn.execute(
                            """UPDATE heartbeat_state SET
                               consecutive_failures=consecutive_failures+1,status='failed',
                               pause_reason='heartbeat_execution_failed',updated_at=?
                               WHERE state_key='default'""",
                            (started_stamp,),
                        )
                    except Exception as failure_write_error:
                        _safe_rollback(conn)
                        return _failure_result(
                            run_id, bucket, "heartbeat_execution_failed", recovered=recovered,
                            database_error_category=(
                                database_category or _database_error_category(failure_write_error)
                            ),
                        )
                    result = _failure_result(
                        run_id, bucket, "heartbeat_execution_failed", recovered=recovered,
                        database_error_category=database_category,
                    )

                return _commit_transaction(
                    conn, canonical_path, dedupe_key, input_fingerprint, result,
                )
            except Exception as error:
                _safe_rollback(conn)
                return _failure_result(
                    run_id, bucket, "heartbeat_execution_failed", recovered=recovered,
                    database_error_category=_database_error_category(error),
                )
