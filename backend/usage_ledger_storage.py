"""Signed snapshot cursors, streaming exports and explicit ledger-only retention."""
import base64
from contextlib import closing
import hashlib
import hmac
import json

from .usage_ledger import LedgerError,export_records,identifier,model_name,raw_json,timestamp


def page(ledger,mode,sid=None,*,model=None,limit=25,cursor=None):
    if mode not in ('api','codex') or type(limit) is not int or not 1<=limit<=200:raise LedgerError('ledger_page_invalid')
    if sid is not None:identifier(sid)
    if model is not None:model_name(model)
    with closing(ledger.connect()) as conn:
        conn.execute('BEGIN')
        meta=dict(conn.execute('SELECT key,value FROM usage_ledger_meta'))
        key=bytes.fromhex(meta['cursor_key']);revision=int(meta['revision'])
        bound={'v':1,'mode':mode,'session':sid,'model':model,'limit':limit,'revision':revision}
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or len(cursor)>4096:raise ValueError()
                data,signature=cursor.split('.')
                if not hmac.compare_digest(hmac.new(key,data.encode(),hashlib.sha256).hexdigest(),signature):raise ValueError()
                token=json.loads(base64.urlsafe_b64decode(data+'='*(-len(data)%4)))
                if not isinstance(token,dict):raise ValueError()
                if any(token.get(k)!=v for k,v in bound.items()):raise ValueError()
                upper=token['upper'];before=token['before']
                if type(upper) is not int or upper<0 or not isinstance(before,list) or len(before)!=2 or type(before[1]) is not int:raise ValueError()
                timestamp(before[0])
            except (ValueError,TypeError,KeyError,UnicodeError):raise LedgerError('ledger_cursor_invalid_or_changed') from None
        else:
            upper=conn.execute('SELECT COALESCE(MAX(id),0) FROM usage_ledger_sequence').fetchone()[0];before=None
        where=['r.mode=?','s.id<=?'];args=[mode,upper]
        if sid is not None:where.append('r.session_id=?');args.append(sid)
        if model is not None:where.append("json_extract(r.payload,'$.model')=?");args.append(model)
        if before:where.append('(r.at<? OR (r.at=? AND s.id<?))');args.extend([before[0],before[0],before[1]])
        query='''SELECT r.payload,r.at,s.id FROM usage_ledger r JOIN usage_ledger_sequence s
                 ON s.mode=r.mode AND s.provider_id=r.provider_id AND s.request_id=r.request_id
                 WHERE '''+' AND '.join(where)+' ORDER BY r.at DESC,s.id DESC LIMIT ?'
        rows=conn.execute(query,[*args,limit+1]).fetchall();next_cursor=None
        if len(rows)>limit:
            last=rows[limit-1];payload={**bound,'upper':upper,'before':[last['at'],last['id']]}
            data=base64.urlsafe_b64encode(raw_json(payload).encode()).decode().rstrip('=')
            next_cursor=data+'.'+hmac.new(key,data.encode(),hashlib.sha256).hexdigest()
        return {'ok':True,'contract_version':1,'mode':mode,'session_id':sid,'model':model,'records':[json.loads(r[0]) for r in rows[:limit]],
                'next_cursor':next_cursor,'limit':limit,'snapshot':upper,'scope':'all' if sid is None else 'session','retention':ledger.retention(mode,conn=conn)}


def stream_export(ledger,mode,sid=None,*,model=None,format='json'):
    if format not in {'json','csv'}:raise LedgerError('ledger_export_invalid')
    # One read transaction spans the stream. New appends do not change this
    # export; no OFFSET, per-page totals, or partial-history invoice claim.
    with closing(ledger.connect()) as conn:
        conn.execute('BEGIN')
        revision=conn.execute("SELECT value FROM usage_ledger_meta WHERE key='revision'").fetchone()[0]
        retention=ledger.retention(mode,conn=conn)
        if format=='json':yield '{"contract_version":1,"retention":'+json.dumps(retention)+',"records":['
        else:yield export_records([],'csv')
        first=True
        for index,record in enumerate(ledger.iter_records(mode,sid,model=model,conn=conn)):
            if index%256==0:
                with closing(ledger.connect()) as check:
                    if check.execute("SELECT value FROM usage_ledger_meta WHERE key='revision'").fetchone()[0]!=revision:raise LedgerError('ledger_export_changed')
            if format=='json':yield ('' if first else ',')+json.dumps(record,ensure_ascii=False,allow_nan=False)
            else:yield export_records([{**record,'_export_scope':retention}],'csv').split('\n',1)[1]
            first=False
        if format=='json':yield ']}'


def maintain(ledger,mode,*,raw_before=None,records_before=None,preview=False):
    """Trusted operator call only. No public destructive maintenance endpoint.

    Raw expiry preserves measured projections/prices. Full expiry changes the
    accounting scope and blocks old-request replay. Neither touches chat/memory.
    """
    if mode not in ('api','codex'):raise LedgerError('ledger_mode_invalid')
    raw_before=timestamp(raw_before) if raw_before is not None else None
    records_before=timestamp(records_before) if records_before is not None else None
    with closing(ledger.connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=ledger.retention(mode,conn=conn)
        raw_before=raw_before or previous['raw_before'];records_before=records_before or previous['records_before']
        for name,value in [('raw_before',raw_before),('records_before',records_before)]:
            if previous[name] and (value is None or value<previous[name]):raise LedgerError('ledger_retention_cannot_restore')
        raw_n=conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE mode=? AND at<? AND COALESCE(json_extract(payload,'$._retention.raw_removed'),0)=0",(mode,raw_before)).fetchone()[0] if raw_before else 0
        record_n=conn.execute('SELECT COUNT(*) FROM usage_ledger WHERE mode=? AND at<?',(mode,records_before)).fetchone()[0] if records_before else 0
        result={'raw_records':raw_n,'records':record_n,'preview':preview}
        if preview:return result
        if raw_before:
            cursor=conn.execute('SELECT rowid,payload FROM usage_ledger WHERE mode=? AND at<?',(mode,raw_before))
            while batch:=cursor.fetchmany(256):
                for row in batch:
                    value=json.loads(row['payload'])
                    if value.get('_retention',{}).get('raw_removed'):continue
                    value['raw_usage']=None
                    if 'usage_baseline' in value:value['usage_baseline']=None
                    value['_retention']={'raw_removed':True,'original_digest':hashlib.sha256(row['payload'].encode()).hexdigest()}
                    conn.execute('UPDATE usage_ledger SET payload=? WHERE rowid=?',(raw_json(value),row['rowid']))
            if mode=='codex' and conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_ledger_manual_compactions'").fetchone():
                conn.execute('UPDATE usage_ledger_manual_compactions SET after_raw=NULL WHERE after_at<?',(raw_before,))
        if records_before:
            conn.execute('INSERT OR IGNORE INTO usage_ledger_removed SELECT mode,provider_id,request_id FROM usage_ledger WHERE mode=? AND at<?',(mode,records_before))
            conn.execute('INSERT OR IGNORE INTO usage_ledger_removed SELECT mode,provider_id,request_id FROM usage_ledger_attempts WHERE mode=? AND at<?',(mode,records_before))
            for table in ('usage_ledger','usage_ledger_attempts','usage_ledger_issues','usage_ledger_events'):
                conn.execute('DELETE FROM '+table+' WHERE mode=? AND at<?',(mode,records_before))
            for table in ('usage_ledger_continuations','usage_ledger_continuation_creations'):
                if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():conn.execute('DELETE FROM '+table+' WHERE mode=? AND created_at<?',(mode,records_before))
            if mode=='codex' and conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_ledger_manual_compactions'").fetchone():conn.execute('DELETE FROM usage_ledger_manual_compactions WHERE created_at<?',(records_before,))
        conn.execute('INSERT OR REPLACE INTO usage_ledger_storage VALUES (?,?,?,?)',(mode,raw_before,records_before,int(bool(records_before or previous['records_removed']))))
        conn.execute("UPDATE usage_ledger_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        conn.commit();return result
