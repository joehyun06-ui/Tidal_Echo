"""Optional manual-task receipts. Local reads and passive numeric events only."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import sqlite3
import uuid

from .usage_ledger import count, raw_json, timestamp
from .usage_ledger_continuation import readonly


class ManualCompactionReceipts:
    def __init__(self,ledger,*,native_db,relay_db,session_row):
        self.ledger,self.native_db,self.relay_db,self.session_row=ledger,native_db,relay_db,session_row
        with closing(ledger.connect()) as conn,conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS usage_ledger_manual_compactions (
                request_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,thread_id TEXT NOT NULL,created_at TEXT NOT NULL,
                before_tokens INTEGER,after_tokens INTEGER,after_at TEXT,after_raw TEXT,dispatched INTEGER NOT NULL DEFAULT 0)''')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_manual_purges(session_id TEXT PRIMARY KEY,at TEXT NOT NULL)')

    def live(self,sid):
        row=self.session_row(sid)
        return isinstance(row,dict) and row.get('id')==sid and row.get('provider')=='codex'

    def task(self,rid):
        with closing(readonly(self.native_db)) as conn:
            row=conn.execute('''SELECT c.* FROM web_codex_compactions c JOIN codex_sessions s
                ON s.api_session=c.session_id WHERE c.request_id=? AND s.status='active' AND s.thread_id=c.thread_id''',(rid,)).fetchone()
            return dict(row) if row and self.live(row['session_id']) else None

    def before(self,row):
        """Existing budget observation, bound to the latest completed native job.

        Read identifiers and numeric payload only, never chat text or memory.
        """
        try:
            with closing(readonly(self.relay_db)) as relay,closing(readonly(self.native_db)) as native:
                found=relay.execute('SELECT payload FROM web_context_observations WHERE session_id=?',(row['session_id'],)).fetchone()
                if not found:return None
                value=json.loads(found[0]);mid=value.get('canonical_message_id')
                last=relay.execute("SELECT MAX(id) FROM messages WHERE direction='in' AND kind IN ('user','voice') AND json_extract(meta,'$.api_session')=?",(row['session_id'],)).fetchone()[0]
                job=native.execute('SELECT canonical_message_id,thread_id,status FROM codex_generation_jobs WHERE api_session=? ORDER BY id DESC LIMIT 1',(row['session_id'],)).fetchone()
                if not job or job['status']!='completed' or job['thread_id']!=row['thread_id'] or job['canonical_message_id']!=mid or mid!=last:return None
                if not value.get('observed_at') or timestamp(value['observed_at'])>timestamp(row['created_at']):return None
                return count(value.get('context_tokens'))
        except (sqlite3.Error,OSError,ValueError,TypeError,AttributeError):return None

    def observe(self,rid):
        row=self.task(rid)
        if not row:return
        uuid.UUID(row['request_id'])
        before=self.before(row) if row['status'] in {'queued','preparing'} else None
        dispatched=row['status'] in {'dispatching','running','unknown','reconciling','reconcile_queued','completed'} or bool(row['turn_id'])
        with closing(self.ledger.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            purge=conn.execute('SELECT at FROM usage_ledger_manual_purges WHERE session_id=?',(row['session_id'],)).fetchone()
            if purge and timestamp(row['created_at'])<=timestamp(purge[0]):return
            conn.execute('INSERT OR IGNORE INTO usage_ledger_manual_compactions(request_id,session_id,thread_id,created_at,before_tokens,dispatched) VALUES (?,?,?,?,?,?)',
                         (rid,row['session_id'],row['thread_id'],row['created_at'],before,int(dispatched)))
            if dispatched:conn.execute('UPDATE usage_ledger_manual_compactions SET dispatched=1 WHERE request_id=?',(rid,))
            conn.commit()
        self.publish(row)

    def publish(self,row):
        if not self.live(row['session_id']):return
        with closing(self.ledger.connect()) as conn:
            saved=conn.execute('SELECT * FROM usage_ledger_manual_compactions WHERE request_id=? AND session_id=? AND thread_id=? AND created_at=?',
                               (row['request_id'],row['session_id'],row['thread_id'],row['created_at'])).fetchone()
        if not saved:return
        status=row['status']
        if status not in {'queued','preparing','dispatching','running','unknown','reconcile_queued','reconciling','completed','failed'}:return
        outcome='completed' if status=='completed' else 'failed' if status=='failed' else 'unknown' if status=='unknown' else 'observed'
        # Original snapshots stay immutable. A late exact-turn usage sample is
        # a separate observation and cannot rewrite completion or claim memory.
        sample=saved['after_at'] if status=='completed' else None
        at=max(timestamp(sample),timestamp(row['updated_at'])) if sample else row['updated_at']
        identity=hashlib.sha256(raw_json([row['request_id'],status,at,saved['before_tokens'],saved['after_tokens'] if status=='completed' else None]).encode()).hexdigest()
        event={'event_id':'manual-compact-'+identity,'session_id':row['session_id'],'mode':'codex','kind':'compaction',
               'status':outcome,'at':at,'source':'codex_manual_task','memory_preserved':None,
               'before_tokens':saved['before_tokens'],'after_tokens':saved['after_tokens'] if status=='completed' else None,
               'snapshot':{'request_id':row['request_id'],'stage':status,'dispatched':bool(saved['dispatched']),
                           'after_usage_observed':sample is not None}}
        self.ledger.event(event,manual_compaction_receipt=(row['request_id'],row['created_at']))

    def receive(self,params):
        if not isinstance(params,dict):return
        thread,turn=params.get('threadId'),params.get('turnId')
        raw=params.get('tokenUsage'); last=raw.get('last') if isinstance(raw,dict) else None
        tokens=count(last.get('totalTokens')) if isinstance(last,dict) else None
        if not isinstance(thread,str) or not isinstance(turn,str) or tokens is None:return
        with closing(readonly(self.native_db)) as conn:
            matches=conn.execute("SELECT request_id FROM web_codex_compactions WHERE thread_id=? AND turn_id=? AND item_done=1 AND status IN ('running','completed')",(thread,turn)).fetchall()
        if len(matches)!=1:return
        row=self.task(matches[0][0])
        if not row:return
        encoded=raw_json(raw)
        now=datetime.now(timezone.utc).isoformat()
        with closing(self.ledger.connect()) as conn,conn:
            # Never recreate a purged receipt from a late notification.
            conn.execute('UPDATE usage_ledger_manual_compactions SET after_tokens=?,after_at=?,after_raw=? WHERE request_id=? AND after_at IS NULL',
                         (tokens,now,encoded,row['request_id']))
        self.publish(row)

    def reconcile(self,sid,mode):
        if mode!='codex' or not self.live(sid):return
        try:
            with closing(readonly(self.native_db)) as conn:
                ids=[r[0] for r in conn.execute('SELECT request_id FROM web_codex_compactions WHERE session_id=?',(sid,))]
            for rid in ids:self.observe(rid)
        except (sqlite3.Error,OSError,ValueError,TypeError):return
