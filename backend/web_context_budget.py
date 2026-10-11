"""Read-only context observations. Never infer a model limit from its name.

Codex 0.160.0 reports last.totalTokens and modelContextWindow. The ratio is
an estimate at that observation, not a quota, billing sum, or next-turn promise.
Only bounded numeric fields and a compaction lifecycle are retained.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from .codex_generation_progress import safe_id

NOTIFICATIONS = frozenset({"thread/tokenUsage/updated", "item/started", "item/completed", "turn/completed"})
MAX_COUNTER = 2**53 - 1


def counter(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNTER else None


def usage_fields(value, *, codex=False):
    if not isinstance(value, dict):
        return {}
    aliases = {
        "input_tokens": ("inputTokens",) if codex else ("input_tokens", "prompt_tokens"),
        "output_tokens": ("outputTokens",) if codex else ("output_tokens", "completion_tokens"),
        "total_tokens": ("totalTokens",) if codex else ("total_tokens",),
    }
    result = {}
    for key, names in aliases.items():
        number = counter(next((value[n] for n in names if n in value), None))
        if number is not None:
            result[key] = number
    return result


@contextmanager
def database(path):
    with closing(sqlite3.connect(path, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS web_context_observations (
            session_id TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()


def delete(conn, sid):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_context_observations'").fetchone():
        conn.execute("DELETE FROM web_context_observations WHERE session_id=?", (sid,))


class CodexBudgetObserver:
    def __init__(self, relay_db, store_path):
        self.relay_db, self.store_path = relay_db, store_path
        self.epoch = uuid.uuid4().hex

    def receive(self, method, params):
        if method not in NOTIFICATIONS or not isinstance(params, dict):
            return
        thread, turn = params.get("threadId"), params.get("turnId")
        if method == "turn/completed":
            turn = params.get("turn", {}).get("id") if isinstance(params.get("turn"), dict) else None
        if not safe_id(thread) or not safe_id(turn):
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") != "contextCompaction" or not safe_id(item.get("id")):
                return
        # Correlate with the actual native thread and current durable generation.
        # Early events may precede the turn/start ACK that records its turn ID.
        with closing(sqlite3.connect(self.store_path, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute("""SELECT j.*,s.model FROM codex_generation_jobs j
                JOIN codex_sessions s ON s.api_session=j.api_session
                WHERE s.status='active' AND s.thread_id=? AND j.thread_id=?
                ORDER BY j.id DESC LIMIT 1""", (thread, thread)).fetchone()
        if not job or not (job["turn_id"] == turn or job["turn_id"] is None and job["status"] == "turn_dispatching"):
            return
        now = datetime.now(timezone.utc).isoformat()
        with database(self.relay_db) as conn:
            # A deleted source must not recreate the observation after deletion.
            source = conn.execute("SELECT 1 FROM messages WHERE id=? AND json_extract(meta,'$.api_session')=?",
                                  (job["canonical_message_id"], job["api_session"])).fetchone()
            if not source:
                return
            row = conn.execute("SELECT payload FROM web_context_observations WHERE session_id=?", (job["api_session"],)).fetchone()
            value = json.loads(row[0]) if row else {}
            if method == "thread/tokenUsage/updated":
                raw = params.get("tokenUsage")
                if not isinstance(raw, dict):
                    return
                usage = usage_fields(raw.get("last"), codex=True)
                window = counter(raw.get("modelContextWindow"))
                value.update(usage=usage, context_tokens=usage.get("total_tokens"),
                             context_window=window if window else None, observed_at=now,
                             canonical_message_id=job["canonical_message_id"], model=job["model"])
            elif method == "turn/completed":
                previous = value.get("compaction") or {}
                if previous.get("status") != "compacting" or previous.get("turn_id") != turn:
                    return
                previous["status"] = "unknown"
                previous["observed_at"] = now
            else:
                previous = value.get("compaction") or {}
                # A duplicate/late started notification cannot undo completion.
                if method == "item/started" and previous.get("item_id") == item["id"] and previous.get("status") == "completed":
                    return
                value["compaction"] = {"status": "compacting" if method == "item/started" else "completed",
                    "item_id": item["id"], "turn_id": turn, "observed_at": now, "epoch": self.epoch}
                # Pre-compaction occupancy is no longer a useful current estimate.
                value["context_tokens"] = None
                value["context_window"] = None
            conn.execute("INSERT OR REPLACE INTO web_context_observations VALUES (?,?)", (job["api_session"], json.dumps(value)))


def read(path, sid, provider, *, epoch=None):
    result = {"ok": True, "contract_version": 1, "api_session": sid, "provider": provider,
              "source": "codex_app_server" if provider == "codex" else "api_response",
              "model": None, "observed_at": None, "usage": {}, "context_tokens": None,
              "context_window": None, "remaining_estimate": None, "used_percent_estimate": None,
              "stale": True, "compaction": None}
    with database(path) as conn:
        if provider == "codex":
            row = conn.execute("SELECT payload FROM web_context_observations WHERE session_id=?", (sid,)).fetchone()
            value = json.loads(row[0]) if row else {}
            for key in ("model", "observed_at", "usage", "context_tokens", "context_window"):
                if key in value:
                    result[key] = value[key]
            compact = value.get("compaction")
            if compact:
                status = compact["status"]
                if status == "compacting" and compact.get("epoch") != epoch:
                    status = "unknown"
                result["compaction"] = {"status": status, "observed_at": compact["observed_at"]}
            message_id = value.get("canonical_message_id")
        else:
            # Latest actual API reply only. Forked historical copies have no new usage.
            row = conn.execute("""SELECT id,ts,meta FROM messages WHERE direction='out' AND kind='reply'
                AND json_extract(meta,'$.api_session')=? ORDER BY id DESC LIMIT 1""", (sid,)).fetchone()
            meta = json.loads(row["meta"]) if row else {}
            api = meta.get("api")
            message_id = None
            if meta.get("source") == "api_generation" and isinstance(api, dict):
                result["usage"] = usage_fields(api.get("usage"))
                result["observed_at"] = row["ts"] if result["usage"] else None
                model = api.get("model")
                result["model"] = model[:160] if isinstance(model, str) else None
                try:
                    message_id = int(meta.get("reply_to"))
                except (ValueError, TypeError):
                    pass
        latest = conn.execute("""SELECT id FROM messages WHERE direction='in' AND kind IN ('user','voice')
            AND json_extract(meta,'$.api_session')=? ORDER BY id DESC LIMIT 1""", (sid,)).fetchone()
        result["stale"] = not (latest and message_id == latest[0] and result["observed_at"])
    used, window = counter(result["context_tokens"]), counter(result["context_window"])
    if used is not None and window:
        result["remaining_estimate"] = max(0, window - used)
        result["used_percent_estimate"] = round(min(100, used / window * 100), 1)
    return result
