"""Immutable API conversation versions; copying history never runs inference.

Use the same session-authority lock as create/rename/delete. A durable request
receipt prevents transport replays from copying a second history. Codex forks
are deliberately unavailable until app-server thread/fork is integrated.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

from .web_session_provider_authority import WebSessionProviderAuthorityError


class ForkError(Exception):
    def __init__(self, category: str, status: int = 409):
        self.category, self.status = category, status


def fork_conversation(authority, session_id: str, body: object, *, relay_db: str) -> dict:
    if not isinstance(body, dict) or set(body) != {"request_id", "message_id", "mode"}:
        raise ForkError("invalid_fork_request", 400)
    try:
        request_id = str(uuid.UUID(body["request_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ForkError("invalid_fork_request", 400) from None
    message_id, mode = body["message_id"], body["mode"]
    if type(message_id) is not int or message_id <= 0 or mode not in {"branch", "edit", "regenerate"}:
        raise ForkError("invalid_fork_request", 400)
    source = next((s for s in authority.session_rows() if s["id"] == session_id), None)
    if source is None:
        raise ForkError("web_session_not_found", 404)
    if source["provider"] != "api":
        raise ForkError("codex_message_fork_unavailable")
    target = "api-fork-" + request_id.replace("-", "")
    if authority.tombstone_for_session(target):
        raise ForkError("web_session_deleted", 410)
    fingerprint = hashlib.sha256(json.dumps([session_id, message_id, mode]).encode()).hexdigest()
    with closing(sqlite3.connect(relay_db, timeout=20)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE IF NOT EXISTS web_session_forks (target TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
        receipt = conn.execute("SELECT fingerprint FROM web_session_forks WHERE target=?", (target,)).fetchone()
        existing = next((s for s in authority.session_rows() if s["id"] == target), None)
        suffix = {"branch": " · 分支", "edit": " · 编辑", "regenerate": " · 新回答"}[mode]
        new = authority.new_row(title=source["title"][:120-len(suffix)]+suffix, provider="api", session_id=target)
        if receipt or existing:
            if not receipt or receipt[0] != fingerprint:
                raise ForkError("fork_request_conflict")
            if not existing:
                # Recover a process exit after SQLite commit but before config
                # publication. The receipt and all copied rows committed together.
                authority.publish_row(new, activate=False)
                existing = new
            return {"ok": True, "created": existing, "duplicate": True}
        rows = conn.execute(
            "SELECT id,ts,direction,kind,text,meta FROM messages "
            "WHERE id <= ? AND json_extract(meta,'$.api_session')=? "
            "AND kind IN ('user','voice','reply') ORDER BY id LIMIT 5001",
            (message_id, session_id),
        ).fetchall()
        if len(rows) > 5000:
            raise ForkError("fork_history_too_large", 413)
        if not rows or rows[-1]["id"] != message_id:
            raise ForkError("fork_message_not_found", 404)
        selected = rows[-1]
        if mode == "edit" and selected["direction"] != "in":
            raise ForkError("fork_message_role_invalid")
        if mode == "regenerate":
            if selected["direction"] != "out":
                raise ForkError("fork_message_role_invalid")
            selected = next((r for r in reversed(rows) if r["direction"] == "in"), None)
            if selected is None:
                raise ForkError("fork_prompt_missing")
        if mode != "branch":
            if json.loads(selected["meta"]).get("attachments"):
                raise ForkError("fork_attachment_resend_unavailable")
            rows = [r for r in rows if r["id"] < selected["id"]]
        if sum(len(r["text"])+len(r["meta"]) for r in rows) > 8*1024*1024:
            raise ForkError("fork_history_too_large", 413)
        with conn:
            conn.execute("INSERT INTO web_session_forks VALUES (?,?)", (target, fingerprint))
            for row in rows:
                original = json.loads(row["meta"])
                meta = {"api_session": target, "source": "web_fork", "branch_origin": {"session_id": session_id, "message_id": row["id"]}}
                if isinstance(original.get("attachments"), list):
                    meta["attachments"] = original["attachments"]
                conn.execute("INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)",
                    (row["ts"], row["direction"], row["kind"], row["text"], json.dumps(meta, ensure_ascii=False)))
        try:
            authority.publish_row(new, activate=False)
        except Exception:
            # No visible session points at this copy if publication fails.
            with conn:
                conn.execute("DELETE FROM messages WHERE json_extract(meta,'$.api_session')=?", (target,))
                conn.execute("DELETE FROM web_session_forks WHERE target=?", (target,))
            raise
    return {"ok": True, "created": new, "duplicate": False}

