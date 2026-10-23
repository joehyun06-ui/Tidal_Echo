"""Default-off continuation receipts; only a trusted server hook may register.

Creating a target is not sending a draft. Success requires the exact registered
draft's first canonical human message AND its completed canonical first reply.
The ledger keeps IDs, stages and keyed digests, never the draft or reply text.
No browser write endpoint, provider call, memory check or production mount.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import uuid

from .usage_ledger import LedgerError, identifier


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


class Continuations:
    def __init__(self, ledger, *, relay_db, session_row, digest_key, native_db=None):
        if not isinstance(digest_key, bytes) or len(digest_key)<32:
            raise LedgerError('ledger_continuation_key_invalid')
        self.ledger, self.relay_db, self.native_db = ledger, relay_db, native_db
        self.session_row, self.key = session_row, digest_key
        self.key_id = hashlib.sha256(digest_key).hexdigest()[:16]
        with closing(ledger.connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS usage_ledger_continuations (
                request_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,target_id TEXT UNIQUE NOT NULL,
                mode TEXT NOT NULL,digest TEXT NOT NULL,key_id TEXT NOT NULL,created_at TEXT NOT NULL,
                input_id INTEGER,received_at TEXT,reply_id INTEGER,ended_at TEXT,result TEXT)''')

    def live(self, sid, mode):
        row = self.session_row(sid)
        return isinstance(row, dict) and row.get('id')==sid and row.get('provider')==mode

    def digest(self, source, target, mode, request, text):
        data=json.dumps([source,target,mode,request,text],ensure_ascii=False,separators=(',',':')).encode()
        return hmac.new(self.key,data,hashlib.sha256).hexdigest()

    def register(self, *, source_id, target_id, mode, request_id, expected_text):
        """Called after authoritative target creation, before any send.

        The caller owns the session creation lock. This is not a public API and
        does not claim a browser-local draft was saved or delivered.
        """
        identifier(source_id); identifier(target_id)
        try: request=uuid.UUID(request_id).hex
        except (ValueError,TypeError,AttributeError): raise LedgerError('ledger_continuation_request_invalid') from None
        if (mode not in ('api','codex') or source_id==target_id or not isinstance(expected_text,str)
                or not expected_text.strip() or len(expected_text.encode('utf-16-le'))//2>12000):
            raise LedgerError('ledger_continuation_request_invalid')
        if not self.live(source_id,mode) or not self.live(target_id,mode):
            raise LedgerError('ledger_continuation_session_unavailable')
        digest=self.digest(source_id,target_id,mode,request,expected_text)
        with closing(self.ledger.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT * FROM usage_ledger_continuations WHERE request_id=? OR target_id=?',(request,target_id)).fetchone()
            if old:
                if tuple(old[:6])!=(request,source_id,target_id,mode,digest,self.key_id):
                    raise LedgerError('ledger_continuation_conflict')
                return False
            with closing(readonly(self.relay_db)) as relay:
                if relay.execute("SELECT 1 FROM messages WHERE json_valid(meta) AND json_extract(meta,'$.api_session')=? LIMIT 1",(target_id,)).fetchone():
                    raise LedgerError('ledger_continuation_target_not_empty')
                if relay.execute("SELECT 1 FROM sqlite_master WHERE name='web_message_versions'").fetchone() and relay.execute('SELECT 1 FROM web_message_versions WHERE version_id=?',(target_id,)).fetchone():
                    raise LedgerError('ledger_continuation_target_is_version')
            conn.execute('INSERT INTO usage_ledger_continuations(request_id,source_id,target_id,mode,digest,key_id,created_at) VALUES (?,?,?,?,?,?,?)',
                         (request,source_id,target_id,mode,digest,self.key_id,datetime.now(timezone.utc).isoformat()))
            conn.commit()
        return True

    def completion(self, relay, row, mid):
        if row['mode']=='api':
            job=relay.execute('SELECT status,reply_id,upstream_unknown FROM api_web_generations WHERE message_id=? AND session_id=?',(mid,row['target_id'])).fetchone()
            if not job: return None,None
            if job['status'] in ('failed','interrupted') and not job['upstream_unknown']: return 'failed',None
            if job['status']!='completed' or job['upstream_unknown'] or not job['reply_id']: return None,None
            reply_id=job['reply_id']; extra={'generation_id':f'api-gen-{mid}'}
        else:
            if self.native_db is None: return None,None
            with closing(readonly(self.native_db)) as native:
                job=native.execute('SELECT status,assistant_message_id,generation_id,callback_identity,client_message_id FROM codex_generation_jobs WHERE api_session=? AND canonical_message_id=?',(row['target_id'],mid)).fetchone()
                if not job: return None,None
                if job['status']=='failed': return 'failed',None
                if job['status']!='completed' or not job['assistant_message_id']: return None,None
                reply_id=job['assistant_message_id']
                extra={'generation_id':job['generation_id'],'codex_callback_identity':job['callback_identity'],'client_message_id':job['client_message_id']}
        reply=relay.execute("SELECT direction,kind,meta,length(trim(text)) AS text_length FROM messages WHERE id=?",(reply_id,)).fetchone()
        if not reply or reply['direction']!='out' or reply['kind']!='reply' or not reply['text_length']: return None,None
        meta=json.loads(reply['meta'])
        if not isinstance(meta,dict): return None,None
        expected={'channel':'web','source':row['mode']+'_generation','provider':row['mode'],
                  'api_session':row['target_id'],'reply_to':str(mid),'finish_reason':'completed',**extra}
        if any(meta.get(k)!=v for k,v in expected.items()) or meta.get('branch_origin') or meta.get('version_copy'): return None,None
        return 'completed',reply_id

    def reconcile(self, source_id, mode):
        """Read durable local receipts; no retries, sending or inference."""
        with closing(self.ledger.connect()) as conn:
            conn.row_factory=sqlite3.Row
            rows=conn.execute('SELECT * FROM usage_ledger_continuations WHERE source_id=? AND mode=?',(source_id,mode)).fetchall()
            for row in rows:
                if not self.live(source_id,mode) or not self.live(row['target_id'],mode): continue
                if row['ended_at'] or row['key_id']!=self.key_id: continue
                try:
                    with closing(readonly(self.relay_db)) as relay:
                        relay.execute('BEGIN')
                        msg=relay.execute("SELECT id,text,meta FROM messages WHERE direction='in' AND kind IN ('user','voice') AND json_valid(meta) AND json_extract(meta,'$.api_session')=? ORDER BY id LIMIT 1",(row['target_id'],)).fetchone()
                        if not msg: continue
                        meta=json.loads(msg['meta'])
                        matched=(meta.get('channel')=='web' and meta.get('source')=='relay' and not meta.get('branch_origin') and not meta.get('version_copy') and
                                 hmac.compare_digest(row['digest'],self.digest(source_id,row['target_id'],mode,row['request_id'],msg['text'])))
                        result,reply_id=None,None
                        if matched:
                            try: result,reply_id=self.completion(relay,row,msg['id'])
                            except (sqlite3.Error,OSError,ValueError,TypeError): pass
                        else: result='content_unverified'
                        if not self.live(source_id,mode) or not self.live(row['target_id'],mode): continue
                        now=datetime.now(timezone.utc).isoformat()
                        with conn:
                            conn.execute('UPDATE usage_ledger_continuations SET input_id=?,received_at=COALESCE(received_at,?),reply_id=?,ended_at=?,result=? WHERE request_id=? AND ended_at IS NULL',
                                         (msg['id'],now,reply_id,now if result else None,result,row['request_id']))
                except (sqlite3.Error,OSError,ValueError,TypeError):
                    continue  # Missing/inconsistent receipts never become success.
            current=conn.execute('SELECT * FROM usage_ledger_continuations WHERE source_id=? AND mode=?',(source_id,mode)).fetchall()
        for row in current:
            if not self.live(source_id,mode) or not self.live(row['target_id'],mode): continue
            stages=[('target_created',row['created_at'],'observed',None)]
            if row['received_at'] and row['result']!='content_unverified': stages.append(('message_received',row['received_at'],'observed',None))
            if row['result']: stages.append(({'completed':'reply_completed','failed':'reply_failed','content_unverified':'content_unverified'}[row['result']],row['ended_at'],
                                             'completed' if row['result']=='completed' else 'failed' if row['result']=='failed' else 'unknown',True if row['result']=='completed' else False if row['result']=='failed' else None))
            for stage,at,status,success in stages:
                self.ledger.event({'event_id':'continuation-'+row['request_id']+'-'+stage,'session_id':source_id,'target_session_id':row['target_id'],
                                   'mode':mode,'at':at,'kind':'continuation','status':status,'source':'canonical_continuation_receipt',
                                   'snapshot':{'stage':stage,'success':success,'input_message_id':row['input_id'] if stage!='target_created' else None,
                                               'reply_message_id':row['reply_id'] if stage=='reply_completed' else None}})
