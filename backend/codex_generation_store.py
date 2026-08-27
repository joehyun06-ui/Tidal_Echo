"""P2-B durable Codex generation session/job state.

This database is intentionally separate from relay.db so provider orchestration does
not mutate the frozen Memory schema authority. Chat text is never stored here; jobs
bind to canonical relay message ids plus SHA-256 digests.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 180
MAX_LEASE_SECONDS = 900
MAX_ERROR_CATEGORY = 96

JOB_STATUSES = frozenset({
    "queued",
    "processing",
    "thread_dispatching",
    "turn_dispatching",
    "in_progress",
    "callback_pending",
    "completed",
    "failed",
    "dispatch_uncertain",
})
ACTIVE_JOB_STATUSES = frozenset(JOB_STATUSES - {"completed", "failed"})
RECOVERY_JOB_STATUSES = frozenset({
    "thread_dispatching",
    "turn_dispatching",
    "in_progress",
    "callback_pending",
    "dispatch_uncertain",
})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CodexGenerationStoreError(RuntimeError):
    """Fixed, data-free persistence failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexGenerationStoreError category={self.category!r}>"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_path(path: str | os.PathLike[str]) -> Path:
    try:
        value = Path(path)
    except (TypeError, ValueError):
        raise CodexGenerationStoreError("codex_generation_store_path_invalid") from None
    if not value.is_absolute() or ".." in value.parts:
        raise CodexGenerationStoreError("codex_generation_store_path_invalid")
    return value


def _safe_id(value: object, category: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise CodexGenerationStoreError(category)
    return value


def _safe_model(value: object, category: str) -> str:
    if not isinstance(value, str) or _SAFE_MODEL.fullmatch(value) is None:
        raise CodexGenerationStoreError(category)
    return value


def _safe_digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CodexGenerationStoreError("codex_generation_input_digest_invalid")
    return value


def _safe_reasoning_effort(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise CodexGenerationStoreError("codex_generation_reasoning_effort_invalid")
    return value


def _safe_error_category(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ERROR_CATEGORY:
        raise CodexGenerationStoreError("codex_generation_error_category_invalid")
    if not value.isascii() or re.fullmatch(r"[a-z0-9_:-]+", value) is None:
        raise CodexGenerationStoreError("codex_generation_error_category_invalid")
    return value


def _safe_canonical_message_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CodexGenerationStoreError("codex_generation_canonical_message_id_invalid")
    return value


def _safe_assistant_message_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CodexGenerationStoreError("codex_generation_assistant_message_id_invalid")
    return value


def _lease_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_LEASE_SECONDS:
        raise CodexGenerationStoreError("codex_generation_lease_invalid")
    return value


def connect(path: str | os.PathLike[str], *, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    database = _validate_path(path)
    if isinstance(timeout_seconds, bool):
        raise CodexGenerationStoreError("codex_generation_store_timeout_invalid")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        raise CodexGenerationStoreError("codex_generation_store_timeout_invalid") from None
    if timeout <= 0 or timeout > 300:
        raise CodexGenerationStoreError("codex_generation_store_timeout_invalid")
    conn = sqlite3.connect(str(database), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}")
    return conn


SCHEMA_SQL = (
    """CREATE TABLE codex_generation_schema (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL)""",
    """CREATE TABLE codex_sessions (
        api_session TEXT PRIMARY KEY,
        provider TEXT NOT NULL CHECK(provider='codex'),
        status TEXT NOT NULL CHECK(status IN ('active','retired')),
        model TEXT NOT NULL,
        model_provider TEXT NOT NULL,
        reasoning_effort TEXT,
        persona_hash TEXT NOT NULL,
        thread_attempt_id TEXT,
        thread_id TEXT,
        cwd TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        retired_at TEXT,
        CHECK(length(persona_hash)=64 AND persona_hash NOT GLOB '*[^0-9a-f]*'),
        CHECK(
            (thread_attempt_id IS NULL AND thread_id IS NULL AND cwd IS NULL)
            OR
            (thread_attempt_id IS NOT NULL AND thread_id IS NOT NULL AND cwd IS NOT NULL)
        ))""",
    """CREATE TABLE codex_generation_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id TEXT NOT NULL UNIQUE,
        callback_identity TEXT NOT NULL UNIQUE,
        client_message_id TEXT NOT NULL UNIQUE,
        api_session TEXT NOT NULL,
        canonical_message_id INTEGER NOT NULL UNIQUE,
        input_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'queued','processing','thread_dispatching','turn_dispatching','in_progress',
            'callback_pending','completed','failed','dispatch_uncertain')),
        lease_until TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count >= 0),
        thread_attempt_id TEXT,
        thread_id TEXT,
        cwd TEXT,
        turn_id TEXT,
        assistant_message_id INTEGER,
        error_category TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(api_session) REFERENCES codex_sessions(api_session),
        CHECK(length(input_digest)=64 AND input_digest NOT GLOB '*[^0-9a-f]*'),
        CHECK(
            (thread_attempt_id IS NULL AND cwd IS NULL)
            OR
            (thread_attempt_id IS NOT NULL AND cwd IS NOT NULL)
        ),
        CHECK(turn_id IS NULL OR thread_id IS NOT NULL),
        CHECK(assistant_message_id IS NULL OR status='completed'))""",
    "CREATE INDEX idx_codex_generation_jobs_claim ON codex_generation_jobs(status,lease_until,id)",
    "CREATE INDEX idx_codex_generation_jobs_session ON codex_generation_jobs(api_session,id)",
)


def initialize(path: str | os.PathLike[str]) -> None:
    database = _validate_path(path)
    try:
        database.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CodexGenerationStoreError("codex_generation_store_unavailable") from None
    try:
        with closing(connect(database)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='codex_generation_schema'"
            ).fetchone()
            if marker is None:
                for statement in SCHEMA_SQL:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO codex_generation_schema(version,applied_at) VALUES(?,?)",
                    (SCHEMA_VERSION, now_iso()),
                )
            else:
                row = conn.execute("SELECT version FROM codex_generation_schema").fetchall()
                if len(row) != 1 or int(row[0]["version"]) != SCHEMA_VERSION:
                    raise CodexGenerationStoreError("codex_generation_store_schema_invalid")
                _validate_schema(conn)
            conn.execute("COMMIT")
    except CodexGenerationStoreError:
        raise
    except sqlite3.Error:
        raise CodexGenerationStoreError("codex_generation_store_unavailable") from None


def _validate_schema(conn: sqlite3.Connection) -> None:
    expected_tables = {"codex_generation_schema", "codex_sessions", "codex_generation_jobs"}
    actual_tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if actual_tables != expected_tables:
        raise CodexGenerationStoreError("codex_generation_store_schema_invalid")
    indexes = {
        str(row["name"])
        for row in conn.execute("PRAGMA index_list(codex_generation_jobs)")
        if not str(row["name"]).startswith("sqlite_autoindex_")
    }
    if indexes != {"idx_codex_generation_jobs_claim", "idx_codex_generation_jobs_session"}:
        raise CodexGenerationStoreError("codex_generation_store_schema_invalid")


def pin_session(
    path: str | os.PathLike[str],
    *,
    api_session: str,
    model: str,
    model_provider: str,
    reasoning_effort: str | None,
    persona_hash: str,
) -> dict:
    api_session = _safe_id(api_session, "codex_generation_session_invalid")
    model = _safe_model(model, "codex_generation_model_invalid")
    model_provider = _safe_model(model_provider, "codex_generation_provider_invalid")
    reasoning_effort = _safe_reasoning_effort(reasoning_effort)
    persona_hash = _safe_digest(persona_hash)
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (api_session,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO codex_sessions
                   (api_session,provider,status,model,model_provider,reasoning_effort,persona_hash,
                    thread_attempt_id,thread_id,cwd,created_at,updated_at,retired_at)
                   VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,NULL)""",
                (
                    api_session, "codex", "active", model, model_provider,
                    reasoning_effort, persona_hash, stamp, stamp,
                ),
            )
        else:
            same = (
                existing["status"] == "active"
                and existing["provider"] == "codex"
                and existing["model"] == model
                and existing["model_provider"] == model_provider
                and existing["reasoning_effort"] == reasoning_effort
                and existing["persona_hash"] == persona_hash
            )
            if not same:
                conn.execute("ROLLBACK")
                raise CodexGenerationStoreError("codex_generation_session_conflict")
        row = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (api_session,)
        ).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def get_session(path: str | os.PathLike[str], api_session: str) -> dict | None:
    api_session = _safe_id(api_session, "codex_generation_session_invalid")
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (api_session,)
        ).fetchone()
    return dict(row) if row is not None else None


def bind_session_thread(
    path: str | os.PathLike[str],
    *,
    job_id: int,
    thread_attempt_id: str,
    thread_id: str,
    cwd: str,
) -> dict:
    thread_attempt_id = _safe_id(thread_attempt_id, "codex_generation_attempt_invalid")
    thread_id = _safe_id(thread_id, "codex_generation_thread_invalid")
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or not Path(cwd).is_absolute():
        raise CodexGenerationStoreError("codex_generation_workspace_invalid")
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job["status"] != "thread_dispatching":
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        if job["thread_attempt_id"] != thread_attempt_id or job["cwd"] != cwd:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        session = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (job["api_session"],)
        ).fetchone()
        if session is None or session["status"] != "active":
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_session_conflict")
        if session["thread_id"] is not None and session["thread_id"] != thread_id:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_session_conflict")
        conn.execute(
            """UPDATE codex_sessions SET thread_attempt_id=?,thread_id=?,cwd=?,updated_at=?
               WHERE api_session=?""",
            (thread_attempt_id, thread_id, cwd, stamp, job["api_session"]),
        )
        conn.execute(
            """UPDATE codex_generation_jobs SET thread_id=?,status='processing',updated_at=?
               WHERE id=?""",
            (thread_id, stamp, job_id),
        )
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def enqueue_job(
    path: str | os.PathLike[str],
    *,
    api_session: str,
    canonical_message_id: int,
    input_digest: str,
    generation_id: str,
    client_message_id: str,
    callback_identity: str,
) -> dict:
    api_session = _safe_id(api_session, "codex_generation_session_invalid")
    canonical_message_id = _safe_canonical_message_id(canonical_message_id)
    input_digest = _safe_digest(input_digest)
    generation_id = _safe_id(generation_id, "codex_generation_id_invalid")
    client_message_id = _safe_id(client_message_id, "codex_generation_client_id_invalid")
    callback_identity = _safe_id(callback_identity, "codex_generation_callback_identity_invalid")
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (api_session,)
        ).fetchone()
        if session is None or session["status"] != "active":
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_session_not_pinned")
        try:
            conn.execute(
                """INSERT INTO codex_generation_jobs
                   (generation_id,callback_identity,client_message_id,api_session,
                    canonical_message_id,input_digest,status,lease_until,attempt_count,recovery_count,
                    thread_attempt_id,thread_id,cwd,turn_id,assistant_message_id,error_category,
                    created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'queued',NULL,0,0,NULL,?,?,NULL,NULL,NULL,?,?)""",
                (
                    generation_id, callback_identity, client_message_id, api_session,
                    canonical_message_id, input_digest, session["thread_id"], session["cwd"],
                    stamp, stamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM codex_generation_jobs WHERE canonical_message_id=?",
                (canonical_message_id,),
            ).fetchone()
            if existing is None or not (
                existing["api_session"] == api_session
                and existing["input_digest"] == input_digest
                and existing["generation_id"] == generation_id
                and existing["client_message_id"] == client_message_id
                and existing["callback_identity"] == callback_identity
            ):
                conn.execute("ROLLBACK")
                raise CodexGenerationStoreError("codex_generation_job_conflict") from None
        row = conn.execute(
            "SELECT * FROM codex_generation_jobs WHERE canonical_message_id=?",
            (canonical_message_id,),
        ).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def get_job(path: str | os.PathLike[str], job_id: int) -> dict | None:
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise CodexGenerationStoreError("codex_generation_job_id_invalid")
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def claim_next_job(
    path: str | os.PathLike[str],
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = 3,
) -> dict | None:
    lease_seconds = _lease_seconds(lease_seconds)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > 20:
        raise CodexGenerationStoreError("codex_generation_max_attempts_invalid")
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE codex_generation_jobs
               SET status='failed',error_category='max_attempts',lease_until=NULL,updated_at=?
               WHERE status IN ('queued','processing') AND attempt_count>=?""",
            (stamp, max_attempts),
        )
        row = conn.execute(
            """SELECT * FROM codex_generation_jobs
               WHERE attempt_count < ? AND (
                    status='queued'
                    OR (status='processing' AND (lease_until IS NULL OR lease_until < ?))
               )
               ORDER BY id LIMIT 1""",
            (max_attempts, stamp),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='processing',lease_until=?,attempt_count=attempt_count+1,
                   error_category=NULL,updated_at=?
               WHERE id=? AND attempt_count < ? AND (
                    status='queued'
                    OR (status='processing' AND (lease_until IS NULL OR lease_until < ?))
               )""",
            (lease_until, stamp, row["id"], max_attempts, stamp),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        claimed = conn.execute(
            "SELECT * FROM codex_generation_jobs WHERE id=?", (row["id"],)
        ).fetchone()
        conn.execute("COMMIT")
    return dict(claimed)


def claim_recovery_job(
    path: str | os.PathLike[str],
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict | None:
    lease_seconds = _lease_seconds(lease_seconds)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
    placeholders = ",".join("?" for _ in RECOVERY_JOB_STATUSES)
    statuses = tuple(sorted(RECOVERY_JOB_STATUSES))
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""SELECT * FROM codex_generation_jobs
                WHERE status IN ({placeholders}) AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY id LIMIT 1""",
            (*statuses, stamp),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        updated = conn.execute(
            f"""UPDATE codex_generation_jobs
                SET lease_until=?,recovery_count=recovery_count+1,updated_at=?
                WHERE id=? AND status IN ({placeholders})
                  AND (lease_until IS NULL OR lease_until < ?)""",
            (lease_until, stamp, row["id"], *statuses, stamp),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        claimed = conn.execute(
            "SELECT * FROM codex_generation_jobs WHERE id=?", (row["id"],)
        ).fetchone()
        conn.execute("COMMIT")
    return dict(claimed)


def begin_thread_dispatch(
    path: str | os.PathLike[str],
    *,
    job_id: int,
    thread_attempt_id: str,
    cwd: str,
) -> dict:
    thread_attempt_id = _safe_id(thread_attempt_id, "codex_generation_attempt_invalid")
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or not Path(cwd).is_absolute():
        raise CodexGenerationStoreError("codex_generation_workspace_invalid")
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='thread_dispatching',thread_attempt_id=?,cwd=?,updated_at=?
               WHERE id=? AND status='processing' AND thread_id IS NULL""",
            (thread_attempt_id, cwd, stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def abandon_thread_attempt_and_requeue(path: str | os.PathLike[str], *, job_id: int) -> dict:
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='queued',lease_until=NULL,thread_attempt_id=NULL,cwd=NULL,
                   thread_id=NULL,turn_id=NULL,error_category=NULL,updated_at=?
               WHERE id=? AND status IN ('thread_dispatching','dispatch_uncertain')
                 AND thread_id IS NULL AND turn_id IS NULL""",
            (stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def begin_turn_dispatch(path: str | os.PathLike[str], *, job_id: int) -> dict:
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job["status"] != "processing":
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        session = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (job["api_session"],)
        ).fetchone()
        if session is None or session["status"] != "active" or not session["thread_id"]:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_thread_missing")
        conn.execute(
            """UPDATE codex_generation_jobs
               SET status='turn_dispatching',thread_attempt_id=?,thread_id=?,cwd=?,updated_at=?
               WHERE id=?""",
            (
                session["thread_attempt_id"], session["thread_id"], session["cwd"],
                stamp, job_id,
            ),
        )
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def record_turn_started(path: str | os.PathLike[str], *, job_id: int, turn_id: str) -> dict:
    turn_id = _safe_id(turn_id, "codex_generation_turn_invalid")
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='in_progress',turn_id=?,updated_at=?
               WHERE id=? AND status='turn_dispatching' AND thread_id IS NOT NULL""",
            (turn_id, stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def record_reconciled_turn(
    path: str | os.PathLike[str],
    *,
    job_id: int,
    turn_id: str,
    status: str,
) -> dict:
    turn_id = _safe_id(turn_id, "codex_generation_turn_invalid")
    if status not in {"inProgress", "completed", "failed", "interrupted"}:
        raise CodexGenerationStoreError("codex_generation_turn_status_invalid")
    target = "in_progress" if status == "inProgress" else (
        "callback_pending" if status == "completed" else "failed"
    )
    error = None if status in {"inProgress", "completed"} else (
        "codex_turn_interrupted" if status == "interrupted" else "codex_turn_failed"
    )
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status=?,turn_id=?,error_category=?,lease_until=NULL,updated_at=?
               WHERE id=? AND status IN
                 ('turn_dispatching','in_progress','dispatch_uncertain','callback_pending')""",
            (target, turn_id, error, stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def requeue_after_verified_turn_absent(path: str | os.PathLike[str], *, job_id: int) -> dict:
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='processing',turn_id=NULL,lease_until=NULL,error_category=NULL,updated_at=?
               WHERE id=? AND status IN ('turn_dispatching','dispatch_uncertain')
                 AND thread_id IS NOT NULL""",
            (stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def mark_dispatch_uncertain(
    path: str | os.PathLike[str],
    *,
    job_id: int,
    category: str = "codex_dispatch_uncertain",
) -> dict:
    category = _safe_error_category(category)
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='dispatch_uncertain',error_category=?,lease_until=NULL,updated_at=?
               WHERE id=? AND status IN ('thread_dispatching','turn_dispatching','in_progress')""",
            (category, stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def mark_callback_pending(path: str | os.PathLike[str], *, job_id: int, turn_id: str) -> dict:
    turn_id = _safe_id(turn_id, "codex_generation_turn_invalid")
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='callback_pending',turn_id=?,lease_until=NULL,error_category=NULL,updated_at=?
               WHERE id=? AND status IN ('in_progress','turn_dispatching','dispatch_uncertain')""",
            (turn_id, stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def mark_completed(
    path: str | os.PathLike[str],
    *,
    job_id: int,
    assistant_message_id: int,
) -> dict:
    assistant_message_id = _safe_assistant_message_id(assistant_message_id)
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        if existing is None:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_job_not_found")
        if existing["status"] == "completed":
            if existing["assistant_message_id"] != assistant_message_id:
                conn.execute("ROLLBACK")
                raise CodexGenerationStoreError("codex_generation_completion_conflict")
        elif existing["status"] == "callback_pending":
            conn.execute(
                """UPDATE codex_generation_jobs
                   SET status='completed',assistant_message_id=?,lease_until=NULL,
                       error_category=NULL,updated_at=? WHERE id=?""",
                (assistant_message_id, stamp, job_id),
            )
        else:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def mark_failed(path: str | os.PathLike[str], *, job_id: int, category: str) -> dict:
    category = _safe_error_category(category)
    stamp = now_iso()
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """UPDATE codex_generation_jobs
               SET status='failed',error_category=?,lease_until=NULL,updated_at=?
               WHERE id=? AND status!='completed'""",
            (category, stamp, job_id),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_state_conflict")
        row = conn.execute("SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)).fetchone()
        conn.execute("COMMIT")
    return dict(row)


def retire_session(path: str | os.PathLike[str], *, api_session: str) -> dict:
    api_session = _safe_id(api_session, "codex_generation_session_invalid")
    stamp = now_iso()
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    statuses = tuple(sorted(ACTIVE_JOB_STATUSES))
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (api_session,)
        ).fetchone()
        if session is None:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_session_not_found")
        active = conn.execute(
            f"""SELECT COUNT(*) AS c FROM codex_generation_jobs
                WHERE api_session=? AND status IN ({placeholders})""",
            (api_session, *statuses),
        ).fetchone()
        if int(active["c"]) != 0:
            conn.execute("ROLLBACK")
            raise CodexGenerationStoreError("codex_generation_session_busy")
        conn.execute(
            """UPDATE codex_sessions SET status='retired',retired_at=?,updated_at=?
               WHERE api_session=?""",
            (stamp, stamp, api_session),
        )
        row = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (api_session,)
        ).fetchone()
        conn.execute("COMMIT")
    return dict(row)
