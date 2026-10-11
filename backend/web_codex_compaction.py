"""Durable manual compaction, serialized with the existing Codex worker.

Never replay a dispatched request. Unknown outcomes keep that native session
frozen until read-only reconciliation finds its completed compaction turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from . import codex_generation_store as store
from .codex_generation_progress import safe_id

EVENTS = frozenset({'turn/started', 'turn/completed', 'item/started', 'item/completed'})
TERMINAL = {'completed', 'failed'}


class CompactionError(ValueError):
    def __init__(self, category, status=409):
        self.category, self.status = category, status
        super().__init__(category)


def request_id(body):
    try:
        if not isinstance(body, dict) or set(body) != {'request_id'}:
            raise ValueError()
        return str(uuid.UUID(body['request_id']))
    except (ValueError, TypeError, AttributeError):
        raise CompactionError('compaction_request_invalid', 400) from None


class Compactions:
    def __init__(self, runtime, *, timeout=180):
        self.runtime, self.path, self.timeout = runtime, runtime.config.store_path, timeout
        self.active, self.finished = None, None
        self.initialized = False
        self.scope = runtime.foundation.runtime.scope(
            methods=frozenset({'thread/compact/start'}), notifications=EVENTS, handler=self.receive)

    def initialize(self):
        if self.initialized:
            return
        with closing(store.connect(self.path)) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS web_codex_compactions (
                request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                status TEXT NOT NULL, turn_id TEXT, item_done INTEGER NOT NULL DEFAULT 0,
                before_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS web_codex_compaction_active ON web_codex_compactions(session_id) WHERE status NOT IN ('completed','failed')")
            # Queued/preparing work had not crossed the mutation boundary.
            conn.execute("UPDATE web_codex_compactions SET status=CASE WHEN status IN ('queued','preparing') THEN 'failed' ELSE 'unknown' END,updated_at=? WHERE status NOT IN ('completed','failed')", (store.now_iso(),))
        self.initialized = True

    def _get(self, conn, sid):
        row = conn.execute('SELECT * FROM web_codex_compactions WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (sid,)).fetchone()
        return dict(row) if row else None

    def status(self, sid):
        with closing(store.connect(self.path)) as conn:
            row = self._get(conn, sid)
            pin = conn.execute('SELECT * FROM codex_sessions WHERE api_session=?', (sid,)).fetchone()
            busy = conn.execute("SELECT 1 FROM codex_generation_jobs WHERE status NOT IN ('completed','failed') LIMIT 1").fetchone()
        ready = bool(pin and pin['status'] == 'active' and pin['thread_id'])
        return {'ok': True, 'contract_version': 1, 'api_session': sid, 'supported': True,
                'can_start': ready and not busy and (not row or row['status'] in TERMINAL),
                'operation': {k: row[k] for k in ('request_id', 'status', 'created_at', 'updated_at')} if row else None}

    def reserve(self, sid, body, *, reconcile=False):
        rid = request_id(body)
        with closing(store.connect(self.path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM web_codex_compactions WHERE request_id=?', (rid,)).fetchone()
            if existing:
                if existing['session_id'] != sid:
                    raise CompactionError('compaction_request_conflict')
                if reconcile and existing['status'] == 'unknown':
                    conn.execute("UPDATE web_codex_compactions SET status='reconcile_queued',updated_at=? WHERE request_id=?", (store.now_iso(),rid))
                conn.commit()
                return self.status(sid)
            if reconcile:
                raise CompactionError('compaction_not_found',404)
            pin = conn.execute('SELECT * FROM codex_sessions WHERE api_session=?', (sid,)).fetchone()
            if not pin or pin['status'] != 'active' or not pin['thread_id']:
                raise CompactionError('compaction_no_thread')
            if store.compaction_pending(conn,sid) or conn.execute("SELECT 1 FROM codex_generation_jobs WHERE status NOT IN ('completed','failed') LIMIT 1").fetchone():
                raise CompactionError('compaction_busy')
            now = store.now_iso()
            conn.execute('INSERT INTO web_codex_compactions(request_id,session_id,thread_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)', (rid,sid,pin['thread_id'],'queued',now,now))
            conn.commit()
        return self.status(sid)

    def update(self, rid, status, **fields):
        allowed = {'turn_id','item_done','before_ids'}
        if set(fields)-allowed:
            raise ValueError('invalid_compaction_fields')
        with closing(store.connect(self.path)) as conn:
            conn.execute('UPDATE web_codex_compactions SET status=?,updated_at=?'+''.join(','+key+'=?' for key in fields)+' WHERE request_id=?', (status,store.now_iso(),*fields.values(),rid))

    def invalidate_budget(self, sid):
        from .web_context_budget import database
        with database(self.runtime.relay_db) as conn:
            conn.execute('DELETE FROM web_context_observations WHERE session_id=?',(sid,))

    def receive(self, method, params):
        if not isinstance(params,dict) or not self.active or params.get('threadId') != self.active['thread_id']:
            return
        rid = self.active['request_id']
        with closing(store.connect(self.path)) as conn:
            row = conn.execute('SELECT * FROM web_codex_compactions WHERE request_id=?',(rid,)).fetchone()
        if not row or row['status'] not in {'dispatching','running','unknown'}:
            return
        turn = params.get('turn')
        tid = turn.get('id') if method.startswith('turn/') and isinstance(turn,dict) else params.get('turnId')
        if not safe_id(tid) or tid in json.loads(row['before_ids']) or row['turn_id'] not in (None,tid):
            return
        item = params.get('item')
        compact = isinstance(item,dict) and item.get('type') == 'contextCompaction'
        if method == 'turn/started' or compact and method in {'item/started','item/completed'}:
            self.update(rid,'running',turn_id=tid,item_done=int(bool(row['item_done']) or compact and method=='item/completed'))
        elif method == 'turn/completed' and row['turn_id'] == tid:
            status = turn.get('status')
            outcome = 'completed' if status == 'completed' and row['item_done'] else 'failed' if status in {'failed','interrupted'} else 'unknown'
            self.update(rid,outcome)
            self.invalidate_budget(row['session_id'])
            if self.finished:
                self.finished.set()

    async def resume(self, pin):
        persona = self.runtime.persona_loader()
        if not isinstance(persona,str) or hashlib.sha256(persona.encode()).hexdigest() != pin['persona_hash']:
            raise CompactionError('compaction_persona_changed')
        return await self.runtime.foundation.generation.resume_thread(thread_id=pin['thread_id'],model=pin['model'],
            model_provider=pin['model_provider'],reasoning_effort=pin['reasoning_effort'],cwd=Path(pin['cwd']),persona=persona)

    def reconcile_page(self, row, page):
        turns = page.get('data', page.get('turns', [])) if isinstance(page,dict) else []
        if not isinstance(turns,list):
            return 'unknown'
        candidates = []
        before = json.loads(row['before_ids'])
        for turn in turns:
            if not isinstance(turn,dict) or not safe_id(turn.get('id')) or turn['id'] in before:
                continue
            if row['turn_id'] and turn['id'] != row['turn_id']:
                continue
            items = turn.get('items',[])
            compact = isinstance(items,list) and any(isinstance(i,dict) and i.get('type')=='contextCompaction' for i in items)
            if compact or row['turn_id']==turn['id'] and row['item_done']:
                candidates.append(turn)
        if len(candidates) != 1:
            return 'unknown'
        status = candidates[0].get('status')
        return 'completed' if status=='completed' else 'failed' if status in {'failed','interrupted'} else 'unknown'

    async def run_once(self):
        with closing(store.connect(self.path)) as conn:
            row = conn.execute("SELECT * FROM web_codex_compactions WHERE status IN ('queued','reconcile_queued') ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return False
        row = dict(row);rid,sid = row['request_id'],row['session_id']
        reconciling = row['status']=='reconcile_queued'
        dispatched = reconciling
        self.update(rid,'reconciling' if reconciling else 'preparing')
        try:
            async with self.runtime.foundation.activity_gate.generation():
                pin = store.get_session(self.path,sid)
                if not pin or pin['status']!='active' or pin['thread_id']!=row['thread_id']:
                    raise CompactionError('compaction_session_changed')
                page = await self.resume(pin)
                if reconciling:
                    outcome = self.reconcile_page(row,page)
                    self.update(rid,outcome)
                    if outcome in TERMINAL:
                        self.invalidate_budget(sid)
                    return True
                turns = page.get('data',page.get('turns',[]))
                if not isinstance(turns,list) or any(t.get('status')=='inProgress' for t in turns if isinstance(t,dict)):
                    raise CompactionError('compaction_busy')
                before = [t['id'] for t in turns if isinstance(t,dict) and safe_id(t.get('id'))]
                self.active,self.finished = row,asyncio.Event()
                self.update(rid,'dispatching',before_ids=json.dumps(before))
                dispatched=True
                self.invalidate_budget(sid)
                result = await self.scope.request('thread/compact/start',{'threadId':pin['thread_id']})
                if result != {}:
                    raise CompactionError('compaction_response_invalid')
                # ACK is not completion. Events can arrive before this response.
                await asyncio.wait_for(self.finished.wait(),timeout=self.timeout)
        except asyncio.CancelledError:
            with closing(store.connect(self.path)) as conn:
                latest=conn.execute('SELECT status FROM web_codex_compactions WHERE request_id=?',(rid,)).fetchone()
            if latest and latest[0] not in TERMINAL:
                self.update(rid,'unknown' if dispatched else 'failed')
            raise
        except Exception:
            # Fail closed without replaying a possibly dispatched mutation.
            with closing(store.connect(self.path)) as conn:
                latest=conn.execute('SELECT status FROM web_codex_compactions WHERE request_id=?',(rid,)).fetchone()
            if latest and latest[0] not in TERMINAL:
                self.update(rid,'unknown' if dispatched else 'failed')
        finally:
            self.active,self.finished=None,None
        return True
