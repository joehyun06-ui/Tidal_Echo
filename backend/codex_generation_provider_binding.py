"""Atomic first-thread/provider binding for a P2-B canary session.

`unresolved` is an internal persistence sentinel only. It is never sent to Codex.
The first successful thread/start freezes the actual modelProvider in the same
SQLite transaction that makes the thread durable for the session/job.
"""

from __future__ import annotations

import re
from contextlib import closing
from pathlib import Path

from . import codex_generation_store as store


UNRESOLVED_MODEL_PROVIDER = "unresolved"
_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def bind_first_thread_and_provider(
    path: str | Path,
    *,
    job_id: int,
    thread_attempt_id: str,
    thread_id: str,
    cwd: str,
    model_provider: str,
) -> dict:
    if not isinstance(model_provider, str) or _SAFE_PROVIDER.fullmatch(model_provider) is None:
        raise store.CodexGenerationStoreError("codex_generation_provider_invalid")
    if model_provider == UNRESOLVED_MODEL_PROVIDER:
        raise store.CodexGenerationStoreError("codex_generation_provider_invalid")
    thread_attempt_id = store._safe_id(  # same private store contract; module is package-internal
        thread_attempt_id, "codex_generation_attempt_invalid"
    )
    thread_id = store._safe_id(thread_id, "codex_generation_thread_invalid")
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or not Path(cwd).is_absolute():
        raise store.CodexGenerationStoreError("codex_generation_workspace_invalid")
    stamp = store.now_iso()
    with closing(store.connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None or job["status"] != "thread_dispatching":
            conn.execute("ROLLBACK")
            raise store.CodexGenerationStoreError("codex_generation_state_conflict")
        if job["thread_attempt_id"] != thread_attempt_id or job["cwd"] != cwd:
            conn.execute("ROLLBACK")
            raise store.CodexGenerationStoreError("codex_generation_state_conflict")
        session = conn.execute(
            "SELECT * FROM codex_sessions WHERE api_session=?", (job["api_session"],)
        ).fetchone()
        if session is None or session["status"] != "active":
            conn.execute("ROLLBACK")
            raise store.CodexGenerationStoreError("codex_generation_session_conflict")
        pinned_provider = session["model_provider"]
        if pinned_provider not in (UNRESOLVED_MODEL_PROVIDER, model_provider):
            conn.execute("ROLLBACK")
            raise store.CodexGenerationStoreError("codex_generation_provider_contract_changed")
        if session["thread_id"] is not None and session["thread_id"] != thread_id:
            conn.execute("ROLLBACK")
            raise store.CodexGenerationStoreError("codex_generation_session_conflict")
        conn.execute(
            """UPDATE codex_sessions
               SET model_provider=?,thread_attempt_id=?,thread_id=?,cwd=?,updated_at=?
               WHERE api_session=?""",
            (
                model_provider, thread_attempt_id, thread_id, cwd, stamp,
                job["api_session"],
            ),
        )
        conn.execute(
            """UPDATE codex_generation_jobs
               SET thread_id=?,status='processing',updated_at=? WHERE id=?""",
            (thread_id, stamp, job_id),
        )
        row = conn.execute(
            "SELECT * FROM codex_generation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        conn.execute("COMMIT")
    return dict(row)
