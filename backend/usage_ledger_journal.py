"""Durable accounting coverage. Never stores prompts, routes or error text."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import uuid

from .usage_ledger import LedgerError,identifier,timestamp

REASONS={'route_unmatched','write_failed','invalid_usage','configuration_unapplied','epoch_changed','pending_evicted','late_event','collector_failure'}


class CollectionJournal:
    def __init__(self,ledger,mode,provider_id=None):
        if mode not in ('api','codex'):raise LedgerError('ledger_mode_invalid')
        self.ledger,self.mode,self.provider_id=ledger,mode,provider_id
        if provider_id is not None:identifier(provider_id)
        self.run_id=uuid.uuid4().hex
        with closing(ledger.connect()) as conn,conn:
            conn.execute('INSERT INTO usage_ledger_runs VALUES (?,?,?,?)',(self.run_id,mode,provider_id,datetime.now(timezone.utc).isoformat()))

    def start(self,record):
        if record.get('mode')!=self.mode:raise LedgerError('ledger_journal_invalid')
        for key in ('provider_id','request_id','session_id'):identifier(record[key])
        at=timestamp(record['at'])
        with closing(self.ledger.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.ledger.blocked(conn,record['mode'],record['session_id'],at) or conn.execute('SELECT 1 FROM usage_ledger_removed WHERE mode=? AND provider_id=? AND request_id=?',(self.mode,record['provider_id'],record['request_id'])).fetchone():return
            old=conn.execute('SELECT session_id FROM usage_ledger_attempts WHERE mode=? AND provider_id=? AND request_id=?',(self.mode,record['provider_id'],record['request_id'])).fetchone()
            if old and old[0]!=record['session_id']:raise LedgerError('ledger_duplicate_conflict')
            state='recorded' if conn.execute('SELECT 1 FROM usage_ledger WHERE mode=? AND provider_id=? AND request_id=?',(self.mode,record['provider_id'],record['request_id'])).fetchone() else 'unconfirmed'
            conn.execute('INSERT OR IGNORE INTO usage_ledger_attempts VALUES (?,?,?,?,?,?,?,?)',
                         (self.mode,record['provider_id'],record['request_id'],record['session_id'],at,state,self.run_id,at))

    def issue(self,reason,*,session_id=None,request_id=None,provider_id=None,at=None):
        if reason not in REASONS:raise LedgerError('ledger_journal_invalid')
        provider=provider_id or self.provider_id or 'unmatched-route';identifier(provider)
        if session_id is not None:identifier(session_id)
        request=request_id or 'issue-'+uuid.uuid4().hex;identifier(request)
        at=timestamp(at or datetime.now(timezone.utc).isoformat())
        key=hashlib.sha256((self.mode+'\0'+provider+'\0'+request+'\0'+reason).encode()).hexdigest()
        with closing(self.ledger.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if session_id and self.ledger.blocked(conn,self.mode,session_id,at):return
            if conn.execute('SELECT 1 FROM usage_ledger_removed WHERE mode=? AND provider_id=? AND request_id=?',(self.mode,provider,request)).fetchone():return
            conn.execute('INSERT OR IGNORE INTO usage_ledger_issues VALUES (?,?,?,?,?,?,?)',(key,self.mode,provider,request,session_id,at,reason))


def coverage(ledger,mode,sid=None,*,conn=None):
    if conn is None:
        with closing(ledger.connect()) as opened:return coverage(ledger,mode,sid,conn=opened)
    where='mode=?'+(' AND session_id=?' if sid is not None else '')
    args=(mode,sid) if sid is not None else (mode,)
    pending=conn.execute('SELECT COUNT(*) FROM usage_ledger_attempts WHERE '+where+" AND state='unconfirmed'",args).fetchone()[0]
    issues=dict(conn.execute('SELECT reason,COUNT(*) FROM usage_ledger_issues WHERE '+where+' GROUP BY reason',args))
    global_issues=conn.execute('SELECT COUNT(*) FROM usage_ledger_issues WHERE mode=? AND session_id IS NULL',(mode,)).fetchone()[0] if sid is not None else 0
    global_gaps=conn.execute("SELECT COUNT(*) FROM usage_ledger_issues WHERE mode=? AND session_id IS NULL AND reason!='configuration_unapplied'",(mode,)).fetchone()[0] if sid is not None else 0
    runs=conn.execute('SELECT COUNT(*),MIN(started_at) FROM usage_ledger_runs WHERE mode=?',(mode,)).fetchone()
    retention=ledger.retention(mode,conn=conn)
    return {'unconfirmed':pending,'issues':issues,'unassigned_issues':global_issues,'observed_runs':runs[0],
            'observed_since':runs[1],'coverage':'observed_retained_records_only',
            'has_observed_gaps':bool(pending or any(k!='configuration_unapplied' for k in issues) or global_gaps),'retention':retention}
