"""Opt-in continuation creation hook. No inference, draft persistence or retry.

The server chooses provider/target and reserves the UUID before crossing the
factory boundary. Uncertain creation is reconciled by reading that exact target;
it is never repeated. Only keyed digests and IDs enter the dedicated ledger.
"""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hmac
import sqlite3
import uuid

from .usage_ledger import LedgerError, identifier
from .usage_ledger_sources import MessageCounts
from .web_continuation import preview


class CreationError(ValueError):
    def __init__(self, category, status=409):
        self.category,self.status=category,status
        super().__init__(category)


class ContinuationCreation:
    def __init__(self, receipts, *, authority, session_lock, create_session):
        self.receipts,self.authority,self.lock,self.factory=receipts,authority,session_lock,create_session
        self.gate=asyncio.Lock()
        self.counts=MessageCounts(receipts.relay_db,session_row=authority.row_for_session)
        with closing(receipts.ledger.connect()) as conn,conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS usage_ledger_continuation_creations (
                request_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,version_id TEXT NOT NULL,
                target_id TEXT UNIQUE NOT NULL,mode TEXT NOT NULL,digest TEXT NOT NULL,key_id TEXT NOT NULL,
                phase TEXT NOT NULL,created_at TEXT NOT NULL)''')

    def source(self,sid,version=None):
        identifier(sid)
        row=self.authority.row_for_session(sid)
        if not row or self.authority.tombstone_for_session(sid): raise CreationError('continuation_source_unavailable',404)
        resolved=self.counts(row['provider'],sid)
        p=resolved['provenance']
        if p.get('status')!='available' or p.get('root_session_id')!=sid: raise CreationError('continuation_selection_unavailable')
        selected=p['selected_session_id']
        if version is not None and selected!=version: raise CreationError('continuation_selection_changed')
        return row,selected

    def preview(self,sid):
        with self.lock:
            row,selected=self.source(sid)
            result=preview(self.receipts.relay_db,selected,row['title'])
            return {**result,'api_session':sid,'registration':{'supported':True,'source_version_id':selected}}

    def parse(self,body):
        if not isinstance(body,dict) or set(body)!={'request_id','source_version_id','text'}: raise CreationError('continuation_request_invalid',400)
        try:
            rid=uuid.UUID(body['request_id']).hex
            version=identifier(body['source_version_id'])
            text=body['text']
            if not isinstance(text,str) or text!=text.strip() or not text or len(text.encode('utf-16-le'))//2>12000: raise ValueError()
        except (ValueError,TypeError,AttributeError,UnicodeError): raise CreationError('continuation_request_invalid',400) from None
        return rid,version,text

    async def create(self,sid,body):
        rid,version,text=self.parse(body)
        async with self.gate:
            with self.lock:
                source,_=self.source(sid,version)
                mode=source['provider']; target=mode+'-continuation-'+rid
                digest=self.receipts.digest(sid,target,mode,rid,text)
                with closing(self.receipts.ledger.connect()) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    old=conn.execute('SELECT * FROM usage_ledger_continuation_creations WHERE request_id=?',(rid,)).fetchone()
                    if old:
                        if (old['source_id'],old['version_id'],old['target_id'],old['mode'],old['key_id'])!=(sid,version,target,mode,self.receipts.key_id) or not hmac.compare_digest(old['digest'],digest):
                            raise CreationError('continuation_request_conflict')
                    else:
                        if self.authority.row_for_session(target) or self.authority.tombstone_for_session(target): raise CreationError('continuation_target_conflict')
                        now=datetime.now(timezone.utc).isoformat()
                        conn.execute('INSERT INTO usage_ledger_continuation_creations VALUES (?,?,?,?,?,?,?,?,?)',
                                     (rid,sid,version,target,mode,digest,self.receipts.key_id,'reserved',now))
                    conn.commit()
                with closing(self.receipts.ledger.connect()) as conn:
                    pending=conn.execute('SELECT * FROM usage_ledger_continuation_creations WHERE request_id=?',(rid,)).fetchone()
                    if not pending:raise CreationError('continuation_creation_uncertain',503)
                    row=dict(pending)
                    # The creating marker commits before the factory is invoked.
                    claimed=conn.execute("UPDATE usage_ledger_continuation_creations SET phase='creating' WHERE request_id=? AND phase='reserved'",(rid,)).rowcount
                    conn.commit()
            if claimed:
                try:
                    # Same provider factory as ordinary sessions. It may pin a
                    # native session, but never sends a draft or starts a turn.
                    await self.factory(provider=mode,title=source['title'][:100]+' · 续窗',since_id=0,activate=False,session_id=target)
                except asyncio.CancelledError:
                    self.uncertain(rid); raise
                except Exception:
                    self.uncertain(rid)
                    raise CreationError('continuation_creation_uncertain',503) from None
            with self.lock:
                self.source(sid,version)
                created=self.authority.row_for_session(target)
                if self.authority.tombstone_for_session(target) or not created or created['provider']!=mode:
                    raise CreationError('continuation_creation_uncertain',503)
                try:
                    self.receipts.register(source_id=sid,target_id=target,mode=mode,request_id=rid,expected_text=text,
                                           creation_receipt=(rid,row['created_at']))
                except LedgerError: raise CreationError('continuation_registration_unconfirmed',503) from None
                with closing(self.receipts.ledger.connect()) as conn,conn:
                    receipt=conn.execute('SELECT 1 FROM usage_ledger_continuations WHERE request_id=? AND source_id=? AND target_id=?',(rid,sid,target)).fetchone()
                    if not receipt: raise CreationError('continuation_registration_unconfirmed',503)
                    conn.execute("UPDATE usage_ledger_continuation_creations SET phase='registered' WHERE request_id=?",(rid,))
                return {'ok':True,'contract_version':1,'api_session':sid,'registered':True,'created':created}

    def uncertain(self,rid):
        with closing(self.receipts.ledger.connect()) as conn,conn:
            conn.execute("UPDATE usage_ledger_continuation_creations SET phase='unknown' WHERE request_id=? AND phase='creating'",(rid,))
