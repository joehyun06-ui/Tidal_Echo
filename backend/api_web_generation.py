"""Durable, at-most-once API Web runs. No provider retries or memory policy changes."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

TERMINAL = {"completed", "interrupted", "failed"}
MAX_TEXT = 64000


class ApiGenerationError(RuntimeError):
    def __init__(self, category, status_code=409):
        super().__init__(category)
        self.category, self.status_code = category, status_code


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


class Store:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self):
        with self.db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS api_web_generations (
                message_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
                ended_at REAL, model TEXT, error TEXT, upstream_unknown INTEGER NOT NULL DEFAULT 0,
                reply_id INTEGER, notified INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS api_web_generation_session ON api_web_generations(session_id,message_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS api_web_generation_active ON api_web_generations(session_id) WHERE status IN ('queued','running','stopping')")
            conn.execute("CREATE TABLE IF NOT EXISTS api_web_closed_sessions (session_id TEXT PRIMARY KEY)")

    def recover(self):
        with self.db() as conn:
            conn.execute("""UPDATE api_web_generations SET status='failed', ended_at=?,
                error='api_process_restarted', upstream_unknown=(started_at IS NOT NULL)
                WHERE status IN ('queued','running','stopping')""", (time.time(),))

    def accept(self, session_id, text, meta):
        """Reserve both canonical input and job in one transaction, before dispatch."""
        with self.db() as conn:
            self._expire_queue(conn, session_id)
            if conn.execute("SELECT 1 FROM api_web_closed_sessions WHERE session_id=?", (session_id,)).fetchone():
                raise ApiGenerationError("generation_not_found", 404)
            if conn.execute("SELECT 1 FROM api_web_generations WHERE session_id=? AND status IN ('queued','running','stopping')", (session_id,)).fetchone():
                raise ApiGenerationError("generation_busy")
            if conn.execute("SELECT count(*) FROM api_web_generations WHERE status IN ('queued','running','stopping')").fetchone()[0] >= 32:
                raise ApiGenerationError("generation_unavailable", 503)
            now = time.time()
            cur = conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user',?,?)", (stamp(now), text, json.dumps(meta, ensure_ascii=False)))
            mid = cur.lastrowid
            meta = {**meta, "api_generation_id": f"api-gen-{mid}"}
            conn.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), mid))
            conn.execute("INSERT INTO api_web_generations(message_id,session_id,status,created_at) VALUES (?,?,'queued',?)", (mid, session_id, now))
            return {"id": mid, "ts": stamp(now), "direction": "in", "kind": "user", "text": text, "meta": meta}

    def _expire_queue(self, conn, session_id):
        # A delayed/failed internal dispatch must never leave the composer stuck.
        # Expired jobs cannot later be claimed; no inference is repeated.
        conn.execute("""UPDATE api_web_generations SET status='failed',ended_at=?,error='api_dispatch_not_started'
            WHERE session_id=? AND status='queued' AND created_at<?""", (time.time(), session_id, time.time() - 45))

    def get(self, session_id, mid=None):
        with self.db() as conn:
            self._expire_queue(conn, session_id)
            query = "SELECT * FROM api_web_generations WHERE session_id=?"
            params = [session_id]
            if mid is not None:
                query += " AND message_id=?"; params.append(mid)
            row = conn.execute(query + " ORDER BY message_id DESC LIMIT 1", params).fetchone()
            return dict(row) if row else None

    def claim(self, sid, mid, expected_text):
        with self.db() as conn:
            self._expire_queue(conn, sid)
            row = conn.execute("SELECT * FROM api_web_generations WHERE session_id=? AND message_id=?", (sid, mid)).fetchone()
            if row is None:
                raise ApiGenerationError("generation_not_found", 404)
            source = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            meta = json.loads(source["meta"]) if source else {}
            if not source or source["direction"] != "in" or source["kind"] != "user" or source["text"] != expected_text or any(meta.get(k) != v for k, v in {"api_session": sid, "channel": "web", "source": "relay"}.items()):
                raise ApiGenerationError("generation_request_invalid", 400)
            changed = conn.execute("UPDATE api_web_generations SET status='running',started_at=? WHERE message_id=? AND status='queued'", (time.time(), mid)).rowcount
            return bool(changed)

    def route(self, mid, model):
        with self.db() as conn:
            conn.execute("UPDATE api_web_generations SET model=? WHERE message_id=? AND status='running'", (str(model or '')[:160], mid))

    def stop(self, sid, mid):
        with self.db() as conn:
            row = conn.execute("SELECT * FROM api_web_generations WHERE session_id=? AND message_id=?", (sid, mid)).fetchone()
            if row is None:
                raise ApiGenerationError("generation_not_found", 404)
            if row["status"] == "queued":
                conn.execute("UPDATE api_web_generations SET status='interrupted',ended_at=? WHERE message_id=?", (time.time(), mid))
                return False
            if row["status"] == "running":
                conn.execute("UPDATE api_web_generations SET status='stopping',upstream_unknown=1 WHERE message_id=?", (mid,))
                return True
            return False

    def finish(self, sid, mid, *, status, text='', api=None, error=None, unknown=False):
        if status not in TERMINAL or not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ApiGenerationError("generation_response_invalid", 502)
        with self.db() as conn:
            row = conn.execute("SELECT * FROM api_web_generations WHERE session_id=? AND message_id=?", (sid, mid)).fetchone()
            if not row or row["status"] in TERMINAL:
                return
            now = time.time(); reply_id = None
            if text.strip():
                # Still require the canonical source; a deletion cannot resurrect content.
                source = conn.execute("SELECT kind,meta FROM messages WHERE id=?", (mid,)).fetchone()
                if not source or source["kind"] != "user" or json.loads(source["meta"]).get("api_session") != sid:
                    raise ApiGenerationError("generation_not_found", 404)
                meta = {"channel": "web", "source": "api_generation", "provider": "api",
                        "api_session": sid, "reply_to": str(mid), "generation_id": f"api-gen-{mid}",
                        "stream_id": f"api-gen-{mid}", "finish_reason": status,
                        "api": api or {"model": row["model"], "usage": {}},
                        "generation": {"started_at": stamp(row["started_at"]), "ended_at": stamp(now),
                                       "elapsed_ms": round(max(0, now - (row["started_at"] or now)) * 1000)}}
                reply_id = conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'out','reply',?,?)", (stamp(now), text, json.dumps(meta, ensure_ascii=False))).lastrowid
            conn.execute("UPDATE api_web_generations SET status=?,ended_at=?,reply_id=?,error=?,upstream_unknown=? WHERE message_id=?", (status, now, reply_id, error, int(unknown), mid))

    def notification(self, sid, mid):
        with self.db() as conn:
            job = conn.execute("SELECT * FROM api_web_generations WHERE session_id=? AND message_id=?", (sid, mid)).fetchone()
            row = conn.execute("SELECT * FROM messages WHERE id=?", (job["reply_id"],)).fetchone() if job else None
            if not row:
                raise ApiGenerationError("generation_not_found", 404)
            conn.execute("UPDATE api_web_generations SET notified=1 WHERE message_id=?", (mid,))
            msg = dict(row); msg["meta"] = json.loads(msg["meta"])
            return {"message": msg, "duplicate": bool(job["notified"])}


def assert_idle(conn, session_id, *, deleting=False):
    """Use the caller's transaction: deleting chat also deletes its run metadata."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_web_generations'").fetchone():
        return
    if conn.execute("SELECT 1 FROM api_web_generations WHERE session_id=? AND status IN ('queued','running','stopping')", (session_id,)).fetchone():
        raise ApiGenerationError("generation_busy")
    if deleting:
        conn.execute("INSERT OR IGNORE INTO api_web_closed_sessions VALUES (?)", (session_id,))
        conn.execute("DELETE FROM api_web_generations WHERE session_id=?", (session_id,))


class ApiWebRuntime:
    def __init__(self, legacy):
        self.legacy, self.store = legacy, Store(legacy.RELAY_DB)
        self.tasks, self.providers, self.progress = {}, {}, {}
        self.epoch = uuid.uuid4().hex
        self.closing = False

    def start(self):
        self.store.initialize(); self.store.recover()

    async def close(self):
        self.closing = True
        for task in list(self.providers.values()): task.cancel()
        if self.tasks: await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def status(self, sid, mid=None):
        job = self.store.get(sid, mid)
        generation = snapshot = None
        if job:
            mid = job["message_id"]
            terminal = job["status"] in TERMINAL
            generation = {"id": f"api-gen-{mid}", "provider": "api", "canonical_message_id": mid,
                          "status": job["status"], "terminal": terminal,
                          "can_stop": job["status"] == "queued" or job["status"] == "running" and mid in self.providers,
                          "assistant_message_id": job["reply_id"], "model": job["model"],
                          "created_at": stamp(job["created_at"]), "started_at": stamp(job["started_at"]),
                          "ended_at": stamp(job["ended_at"]), "error": job["error"],
                          "cancel_scope": "local_request", "upstream_result_unknown": bool(job["upstream_unknown"])}
            if job["started_at"]:
                generation["elapsed_ms"] = round(max(0, (job["ended_at"] or time.time()) - job["started_at"]) * 1000)
            if not terminal: snapshot = self.progress.get(mid)
        return {"ok": True, "contract_version": 1, "provider": "api", "api_session": sid, "generation": generation, "snapshot": snapshot}

    async def stop(self, sid, generation_id):
        try:
            mid = int(generation_id.removeprefix("api-gen-"))
        except (ValueError, AttributeError):
            raise ApiGenerationError("generation_request_invalid", 400) from None
        if mid <= 0 or generation_id != f"api-gen-{mid}":
            raise ApiGenerationError("generation_request_invalid", 400)
        changed = self.store.stop(sid, mid)
        if changed:
            task = self.providers.get(mid)
            if task is not None: task.cancel()
            elif mid not in self.tasks: self.store.finish(sid, mid, status="failed", error="api_request_unavailable", unknown=True)
        return self.status(sid, mid)

    async def ingest(self, body):
        sid = str(body.get("session_id") or body.get("api_session") or '')
        mid = body.get("id")
        if isinstance(mid, bool) or not isinstance(mid, int) or mid <= 0:
            raise ApiGenerationError("generation_request_invalid", 400)
        text = str(body.get("text") or body.get("message") or '').strip()
        if self.closing: raise ApiGenerationError("generation_unavailable", 503)
        if self.store.claim(sid, mid, text):
            task = asyncio.create_task(self._run(sid, mid, text))
            self.tasks[mid] = task
            def settled(done):
                self.tasks.pop(mid, None)
                if not done.cancelled(): done.exception()
            task.add_done_callback(settled)
        return {"ok": True, "queued": True, "provider": "api", "generation_provider": "api",
                "generation_id": f"api-gen-{mid}", "api_session": sid, "canonical_message_id": mid, "status": "queued"}

    async def _run(self, sid, mid, text):
        gid = f"api-gen-{mid}"
        partial = ''
        async def sink(chunk):
            nonlocal partial
            partial = (partial + chunk)[:MAX_TEXT]
            previous = self.progress.get(mid)
            self.progress[mid] = {"type": "reply_snapshot", "provider": "api", "api_session": sid,
                "generation_id": gid, "stream_id": gid, "canonical_message_id": mid, "epoch": self.epoch,
                "revision": (previous["revision"] if previous else 0) + 1, "text": partial,
                "ts": previous["ts"] if previous else stamp(time.time())}
            await self.legacy.relay_out({**self.progress[mid], "type": "reply_delta", "snapshot": True, "delta": chunk})
        try:
            # Stop can arrive after admission but before the worker has started.
            job = self.store.get(sid, mid)
            if not job or job["status"] != "running" or self.closing:
                self.store.finish(sid, mid, status="interrupted" if not self.closing else "failed", error="api_process_restarted" if self.closing else None)
                return
            messages = self.legacy.build_ingest_messages(text, msg_id=mid, session_id=sid)
            task = asyncio.create_task(self.legacy.run_model(messages, stream_id=gid, session_id=sid,
                emit_stream=True, progress_sink=sink, on_route=lambda route: self.store.route(mid, route.get('model'))))
            self.providers[mid] = task
            try:
                out = await task
            except asyncio.CancelledError:
                self.store.finish(sid, mid, status="failed" if self.closing else "interrupted", text=partial,
                                  error="api_process_restarted" if self.closing else None, unknown=True)
            else:
                success = out.get("outcome") == "success"
                api = {"runtime": "api_loop", "model": out.get("model"), "usage": out.get("usage") or {}, "fallback_from": out.get("tried") or [], "session": sid}
                self.store.finish(sid, mid, status="completed" if success else "failed",
                    text=(out.get("text") or '') if success else partial, api=api,
                    error=None if success else "api_provider_incomplete", unknown=out.get("outcome") == "dispatch_uncertain")
            job = self.store.get(sid, mid)
            if job and job["reply_id"]:
                # Persistence is authoritative. Failed/lost fan-out is repaired by history GET, never inference.
                await self.legacy.relay_out({"type": "reply", "source": "api_generation", "provider": "api", "channel": "web", "api_session": sid, "generation_id": gid})
        except Exception:
            self.store.finish(sid, mid, status="failed", error="api_generation_unavailable", unknown=True)
        finally:
            self.providers.pop(mid, None); self.progress.pop(mid, None)
