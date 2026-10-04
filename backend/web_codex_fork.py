"""Native Codex conversation versions, with no inference during a fork.

The relay owns copies of visible messages; Codex owns model context. A durable
receipt prevents replaying an uncertain native RPC. Turn bindings for copied
messages live separately from usage/jobs, so copies cannot count as new work.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

from . import codex_generation_store as store
from .codex_generation_protocol import CodexGenerationError
from .web_session_fork import ForkError


def _request(body):
    if not isinstance(body, dict) or set(body) != {"request_id", "message_id", "mode"}:
        raise ForkError("invalid_fork_request", 400)
    try:
        request_id = str(uuid.UUID(body["request_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ForkError("invalid_fork_request", 400) from None
    if (type(body["message_id"]) is not int or body["message_id"] <= 0
            or body["mode"] not in {"branch", "edit", "regenerate"}):
        raise ForkError("invalid_fork_request", 400)
    return "codex-fork-" + request_id.replace("-", "")


def _tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS web_codex_forks (
        target TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        phase TEXT NOT NULL, native TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_codex_fork_turns (
        message_id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL)""")


def _source(authority, session_id, target):
    if authority.tombstone_for_session(target):
        raise ForkError("web_session_deleted", 410)
    source = authority.row_for_session(session_id)
    if not source:
        raise ForkError("web_session_not_found", 404)
    if source["provider"] != "codex":
        raise ForkError("fork_provider_mismatch")
    return source


def _pin(store_path, session_id):
    pin = store.get_session(store_path, session_id)
    if not pin or pin["status"] != "active":
        raise ForkError("codex_session_unavailable")
    with closing(store.connect(store_path)) as conn:
        busy = conn.execute(
            "SELECT 1 FROM codex_generation_jobs WHERE api_session=? "
            "AND status NOT IN ('completed','failed') LIMIT 1", (session_id,),
        ).fetchone()
    if busy:
        raise ForkError("codex_generation_busy")
    return pin


def _prefix(conn, store_path, session_id, body):
    rows = conn.execute(
        "SELECT id,ts,direction,kind,text,meta FROM messages WHERE id<=? "
        "AND json_extract(meta,'$.api_session')=? "
        "AND kind IN ('user','voice','reply') ORDER BY id LIMIT 5001",
        (body["message_id"], session_id),
    ).fetchall()
    if len(rows) > 5000 or sum(len(r["text"])+len(r["meta"]) for r in rows) > 8*1024*1024:
        raise ForkError("fork_history_too_large", 413)
    if not rows or rows[-1]["id"] != body["message_id"]:
        raise ForkError("fork_message_not_found", 404)
    selected, mode = rows[-1], body["mode"]
    if (mode == "edit" and selected["direction"] != "in"
            or mode == "regenerate" and selected["direction"] != "out"):
        raise ForkError("fork_message_role_invalid")
    draft = None
    if mode == "regenerate":
        prompt = next((r for r in reversed(rows) if r["direction"] == "in"), None)
        if not prompt:
            raise ForkError("fork_prompt_missing")
    else:
        prompt = selected
    # A native fork ends at a completed turn. Branching from a user message
    # therefore keeps that question as a draft instead of including its answer.
    if mode != "branch" or selected["direction"] == "in":
        if json.loads(prompt["meta"]).get("attachments"):
            raise ForkError("fork_attachment_resend_unavailable")
        if mode == "branch":
            draft = prompt["text"]
        kept = [r for r in rows if r["id"] < prompt["id"]]
    else:
        kept = rows
    bindings = {}
    with closing(store.connect(store_path)) as jobs:
        for row in rows:
            copied = conn.execute(
                "SELECT turn_id FROM web_codex_fork_turns WHERE message_id=?", (row["id"],),
            ).fetchone()
            job = jobs.execute(
                "SELECT turn_id,status FROM codex_generation_jobs WHERE api_session=? "
                "AND (canonical_message_id=? OR assistant_message_id=?)",
                (session_id, row["id"], row["id"]),
            ).fetchone()
            if copied:
                bindings[row["id"]] = copied["turn_id"]
            elif job and job["status"] == "completed" and job["turn_id"]:
                bindings[row["id"]] = job["turn_id"]
    # Do not silently skip failed/missing native turns or invent context from UI.
    validate = rows if mode == "regenerate" else kept
    if len(validate) % 2:
        raise ForkError("codex_fork_history_unavailable")
    for offset in range(0, len(validate), 2):
        human, reply = validate[offset:offset+2]
        if (human["direction"] != "in" or reply["direction"] != "out"
                or not bindings.get(human["id"])
                or bindings.get(human["id"]) != bindings.get(reply["id"])):
            raise ForkError("codex_fork_history_unavailable")
    return kept, bindings, draft


def _bind_target(store_path, target, source_pin, native):
    source_pin = native.get("contract", source_pin)
    pin = store.pin_session(
        store_path, api_session=target, model=source_pin["model"],
        model_provider=source_pin["model_provider"],
        reasoning_effort=source_pin["reasoning_effort"], persona_hash=source_pin["persona_hash"],
    )
    if not native.get("thread_id"):
        return
    if pin["thread_id"] not in (None, native["thread_id"]):
        raise ForkError("fork_request_conflict")
    with closing(store.connect(store_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE codex_sessions SET thread_id=?,thread_attempt_id=?,cwd=?,updated_at=? "
                "WHERE api_session=? AND status='active'",
                (native["thread_id"], target, native["cwd"], store.now_iso(), target),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


async def fork_codex_conversation(authority, session_id, body, *, relay_db, runtime, lock):
    if not runtime.generation_enabled:
        raise ForkError("codex_generation_disabled", 503)
    target = _request(body)
    fingerprint = hashlib.sha256(json.dumps([session_id, body["message_id"], body["mode"]]).encode()).hexdigest()
    try:
        async with runtime.foundation.activity_gate.control():
            with lock:
                source = _source(authority, session_id, target)
                pin = _pin(runtime.config.store_path, session_id)
                persona = runtime.persona_loader()
                if (not isinstance(persona, str) or not persona.strip()
                        or hashlib.sha256(persona.encode()).hexdigest() != pin["persona_hash"]):
                    raise ForkError("codex_generation_persona_changed")
                with closing(sqlite3.connect(relay_db, timeout=20)) as conn:
                    conn.row_factory = sqlite3.Row
                    _tables(conn)
                    receipt = conn.execute("SELECT * FROM web_codex_forks WHERE target=?", (target,)).fetchone()
                    if receipt and receipt["fingerprint"] != fingerprint:
                        raise ForkError("fork_request_conflict")
                    existing = authority.row_for_session(target)
                    if existing:
                        if not receipt or receipt["phase"] != "copied" or existing["provider"] != "codex":
                            raise ForkError("fork_request_conflict")
                        result = {"ok": True, "created": existing, "duplicate": True}
                        if body["mode"] == "branch":
                            question = conn.execute("SELECT text FROM messages WHERE id=? AND direction='in' AND json_extract(meta,'$.api_session')=?", (body["message_id"], session_id)).fetchone()
                            if question:
                                result["draft"] = question["text"]
                        return result
                    if receipt and receipt["phase"] == "dispatching":
                        raise ForkError("codex_fork_result_unknown")
                    rows, bindings, draft = _prefix(conn, runtime.config.store_path, session_id, body)
                    native = json.loads(receipt["native"]) if receipt else None
            if not receipt:
                await runtime.foundation.generation.qualify(pin["model"], pin["reasoning_effort"])
                # Persist before crossing the process boundary; no RPC retry on
                # timeout/restart, even if the upstream created an orphan thread.
                with sqlite3.connect(relay_db, timeout=20) as conn:
                    conn.execute("INSERT INTO web_codex_forks VALUES (?,?,?,?)", (target, fingerprint, "dispatching", "{}"))
                native = {"contract": {key: pin[key] for key in ("model", "model_provider", "reasoning_effort", "persona_hash")}}
                if rows:
                    if not pin["thread_id"]:
                        raise ForkError("codex_fork_history_unavailable")
                    result = await runtime.foundation.generation.fork_thread(
                        thread_id=pin["thread_id"], last_turn_id=bindings[rows[-1]["id"]],
                        api_session=target, attempt_id=target, model=pin["model"],
                        model_provider=pin["model_provider"], reasoning_effort=pin["reasoning_effort"], persona=persona,
                    )
                    native.update(thread_id=result.thread_id, cwd=str(result.cwd))
                with sqlite3.connect(relay_db, timeout=20) as conn:
                    conn.execute("UPDATE web_codex_forks SET phase='ready',native=? WHERE target=?", (json.dumps(native), target))
            with lock:
                source = _source(authority, session_id, target)
                current = store.get_session(runtime.config.store_path, session_id)
                if not current or current["status"] != "active" or current["thread_id"] != pin["thread_id"]:
                    raise ForkError("codex_session_unavailable")
                _bind_target(runtime.config.store_path, target, pin, native)
                suffix = {"branch": " · 分支", "edit": " · 编辑", "regenerate": " · 新回答"}[body["mode"]]
                new = authority.new_row(title=source["title"][:120-len(suffix)]+suffix, provider="codex", session_id=target)
                with sqlite3.connect(relay_db, timeout=20) as conn:
                    phase = conn.execute("SELECT phase FROM web_codex_forks WHERE target=?", (target,)).fetchone()[0]
                    if phase != "copied":
                        for row in rows:
                            meta = {"api_session": target, "source": "web_fork", "provider": "codex",
                                    "branch_origin": {"session_id": session_id, "message_id": row["id"]}}
                            copied = conn.execute("INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)",
                                (row["ts"], row["direction"], row["kind"], row["text"], json.dumps(meta, ensure_ascii=False)))
                            conn.execute("INSERT INTO web_codex_fork_turns VALUES (?,?)", (copied.lastrowid, bindings[row["id"]]))
                        conn.execute("UPDATE web_codex_forks SET phase='copied' WHERE target=?", (target,))
                authority.publish_row(new, activate=False)
            result = {"ok": True, "created": new, "duplicate": bool(receipt)}
            if draft is not None:
                result["draft"] = draft
            return result
    except CodexGenerationError as error:
        category = "codex_generation_busy" if error.category == "codex_generation_busy" else "codex_fork_unavailable"
        raise ForkError(category, 409 if category == "codex_generation_busy" else 503) from None
    except store.CodexGenerationStoreError:
        raise ForkError("codex_fork_storage_unavailable", 503) from None
