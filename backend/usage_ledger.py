"""Offline-first usage ledger. No network, environment access or memory imports.

Callers submit terminal request usage, never response bodies or chat text.
Unknown values stay null. API and subscription accounting are disjoint.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

MAX = 2**53 - 1
METRICS = ('input', 'output', 'total', 'reasoning', 'cache_read', 'cache_write', 'cache_miss', 'ordinary_input')
DIALECTS = {'openai_responses', 'openai_chat', 'deepseek_chat', 'anthropic_messages', 'codex', 'unknown'}


class LedgerError(ValueError):
    pass


def count(value):
    return value if type(value) is int and 0 <= value <= MAX else None


def add(*values):
    return count(sum(values)) if values and all(count(v) is not None for v in values) else None


def subtract(value, *parts):
    return count(value-sum(parts)) if count(value) is not None and all(count(v) is not None for v in parts) else None


def pick(value, *path):
    for key in path:
        if not isinstance(value, dict): return None
        value = value.get(key)
    return count(value)


def raw_json(value):
    def check(item, depth=0):
        if depth > 12: raise LedgerError('ledger_raw_invalid')
        if isinstance(item, dict):
            if len(item) > 256: raise LedgerError('ledger_raw_invalid')
            for k, v in item.items():
                if not isinstance(k, str) or len(k) > 200 or k.lower() in {'authorization', 'api_key', 'access_token', 'refresh_token', 'client_secret', 'password', 'headers'}:
                    raise LedgerError('ledger_raw_invalid')
                check(v, depth+1)
        elif isinstance(item, list):
            if len(item) > 256: raise LedgerError('ledger_raw_invalid')
            for v in item: check(v, depth+1)
        elif item is not None and type(item) not in (str, int, float, bool): raise LedgerError('ledger_raw_invalid')
        elif isinstance(item, str) and len(item) > 8192: raise LedgerError('ledger_raw_invalid')
    check(value)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(data.encode()) > 131072: raise LedgerError('ledger_raw_invalid')
    return data


def normalize(raw, dialect, *, usage_baseline=None):
    if dialect not in DIALECTS or raw is not None and not isinstance(raw, dict): raise LedgerError('ledger_usage_invalid')
    raw_json(raw)
    u = raw or {}; m = dict.fromkeys(METRICS); issues = []; paths = {}
    def field(name, *path):
        m[name] = pick(u, *path)
        if m[name] is not None: paths[name] = '.'.join(path)
    if dialect in {'openai_responses', 'openai_chat', 'deepseek_chat'}:
        chat = dialect != 'openai_responses'
        inp, out = ('prompt_tokens', 'completion_tokens') if chat else ('input_tokens', 'output_tokens')
        field('input', inp); field('output', out); field('total', 'total_tokens')
        field('cache_read', inp+'_details', 'cached_tokens')
        field('cache_write', inp+'_details', 'cache_write_tokens')
        field('reasoning', out+'_details', 'reasoning_tokens')
        if dialect == 'deepseek_chat':
            hit = pick(u, 'prompt_cache_hit_tokens')
            if hit is not None:
                if m['cache_read'] is not None and hit != m['cache_read']: issues.append('cache_read_conflict'); m['cache_read'] = None
                else: m['cache_read'] = hit; paths['cache_read'] = 'prompt_cache_hit_tokens'
            field('cache_miss', 'prompt_cache_miss_tokens')
            if m['input'] is not None and m['cache_read'] is not None and m['cache_miss'] is not None and add(m['cache_read'], m['cache_miss']) != m['input']:
                issues.append('cache_partition_conflict'); m['cache_read'] = m['cache_miss'] = None
        if m['cache_miss'] is None and 'cache_partition_conflict' not in issues:
            m['cache_miss'] = subtract(m['input'], m['cache_read'])
            if m['cache_miss'] is not None: paths['cache_miss'] = 'derived:input-cache_read'
        m['ordinary_input'] = subtract(m['input'], m['cache_read'], m['cache_write'])
    elif dialect == 'anthropic_messages':
        field('ordinary_input', 'input_tokens'); field('output', 'output_tokens')
        field('cache_read', 'cache_read_input_tokens'); field('cache_write', 'cache_creation_input_tokens')
        m['input'] = add(m['ordinary_input'], m['cache_read'], m['cache_write'])
        m['cache_miss'] = add(m['ordinary_input'], m['cache_write'])
        paths['input'] = 'derived:input_tokens+cache_read_input_tokens+cache_creation_input_tokens'
    elif dialect == 'codex':
        fields = [('input', 'inputTokens'), ('output', 'outputTokens'), ('total', 'totalTokens'), ('reasoning', 'reasoningOutputTokens')]
        if 'last' in u or 'total' in u:
            # Native last is a response/context snapshot, never a whole-turn sum.
            total = u.get('total') if isinstance(u.get('total'),dict) else {}
            baseline = usage_baseline if isinstance(usage_baseline,dict) else {}
            if usage_baseline is None:
                issues.append('codex_baseline_unavailable')
            else:
                raw_json(usage_baseline)
                reset = any(pick(total,key) is not None and pick(baseline,key) is not None and total[key] < baseline[key] for _,key in fields)
                conflict = any(pick(v,'inputTokens') is not None and pick(v,'outputTokens') is not None and pick(v,'totalTokens') is not None and add(v['inputTokens'],v['outputTokens']) != v['totalTokens'] for v in (total,baseline))
                if reset or conflict:
                    issues.append('codex_counter_reset' if reset else 'codex_counter_conflict')
                else:
                    for name,key in fields:
                        m[name] = subtract(pick(total,key),pick(baseline,key))
                        if m[name] is not None: paths[name] = 'derived:total.'+key+'-baseline.'+key
        else:
            for name,key in fields: field(name,key)
        # Subscription counters never become API cache metrics or API expenses.
    if m['total'] is None:
        m['total'] = add(m['input'], m['output'])
        if m['total'] is not None: paths['total'] = 'derived:input+output'
    elif m['input'] is not None and m['output'] is not None and m['total'] != add(m['input'], m['output']):
        issues.append('total_conflict'); m['total'] = None
    if m['reasoning'] is not None and m['output'] is not None and m['reasoning'] > m['output']:
        issues.append('reasoning_conflict'); m['reasoning'] = None
    for name in ('cache_read', 'cache_write', 'cache_miss'):
        if m[name] is not None and m['input'] is not None and m[name] > m['input']:
            issues.append(name+'_conflict'); m[name] = None
    if m['cache_read'] is not None and m['cache_write'] is not None and m['input'] is not None and m['cache_read']+m['cache_write'] > m['input']:
        issues.append('cache_partition_conflict'); m['cache_read'] = m['cache_write'] = m['cache_miss'] = m['ordinary_input'] = None
    rate = m['cache_read']/m['input'] if m['cache_read'] is not None and m['input'] else None
    return {'metrics': m, 'paths': paths, 'issues': issues, 'cache_hit_rate': rate,
            'cache_applicable': dialect != 'codex', 'raw': raw, 'dialect': dialect}


def number(value):
    try:
        if not isinstance(value, str) or len(value) > 60: return None
        d = Decimal(value)
        return d if d.is_finite() and 0 <= d <= Decimal('1e12') and -12 <= d.as_tuple().exponent <= 12 else None
    except InvalidOperation: return None


def estimate(normalized, price):
    """Explicit per-request price snapshot; never guess a model's current price."""
    unavailable = {'amount': None, 'savings': None, 'currency': None, 'basis': 'unavailable', 'scope': 'token_only'}
    if normalized['dialect'] == 'codex' or not isinstance(price, dict): return unavailable
    if set(price)-{'currency','per_million','write_in_input_rate','price_id','effective_at'}: raise LedgerError('ledger_price_invalid')
    if 'write_in_input_rate' in price and type(price['write_in_input_rate']) is not bool: raise LedgerError('ledger_price_invalid')
    currency = price.get('currency')
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency): raise LedgerError('ledger_price_invalid')
    rates = price.get('per_million')
    if not isinstance(rates, dict) or set(rates)-{'input', 'output', 'cache_read', 'cache_write', 'write_30m', 'write_12h', 'write_5m', 'write_1h'} or any(number(v) is None for v in rates.values()): raise LedgerError('ledger_price_invalid')
    if not {'input','output'} <= set(rates): return {**unavailable,'currency':currency}
    m = normalized['metrics']; ordinary = m['ordinary_input']; writes = m['cache_write']
    if price.get('write_in_input_rate') is True:
        ordinary = m['cache_miss']; writes = 0
    units = {'input': ordinary, 'output': m['output'], 'cache_read': m['cache_read'], 'cache_write': writes}
    raw = normalized['raw'] or {}
    if normalized['dialect'] == 'anthropic_messages':
        split = raw.get('cache_creation')
    else:
        details = raw.get('input_tokens_details' if normalized['dialect']=='openai_responses' else 'prompt_tokens_details')
        split = details.get('cache_write_tokens_details') if isinstance(details,dict) else None
    keys = {'write_5m': 'ephemeral_5m_input_tokens', 'write_1h': 'ephemeral_1h_input_tokens'} if normalized['dialect'] == 'anthropic_messages' else {'write_30m': 'ttl_30m_tokens', 'write_12h': 'ttl_12h_tokens'}
    if writes is not None and writes > 0 and isinstance(split, dict):
        pieces = {name: count(split.get(key)) for name, key in keys.items()}
        if add(*pieces.values()) != writes: return {**unavailable, 'currency': currency}
        units.pop('cache_write'); units.update(pieces)
    if any(v is None or v > 0 and k not in rates for k, v in units.items()): return {**unavailable, 'currency': currency}
    with localcontext() as precision:
        precision.prec=60
        cost = sum(Decimal(v)*number(rates[k]) for k, v in units.items() if v > 0)/Decimal(1000000)
        baseline = (Decimal(m['input'])*number(rates['input'])+Decimal(m['output'])*number(rates['output']))/Decimal(1000000) if m['input'] is not None and m['output'] is not None and 'input' in rates and 'output' in rates else None
        return {'amount': str(cost), 'savings': str(baseline-cost) if baseline is not None else None,
                'currency': currency, 'basis': 'estimated_price_snapshot', 'scope': 'token_only'}


def validate_context(value):
    if value is None: return
    if not isinstance(value, dict) or set(value)-{'tokens','limit','stale','observed_at','source','model'}: raise LedgerError('ledger_context_invalid')
    for k in ('tokens','limit'):
        if value.get(k) is not None and count(value[k]) is None: raise LedgerError('ledger_context_invalid')
    if 'stale' in value and type(value['stale']) is not bool: raise LedgerError('ledger_context_invalid')
    if value.get('observed_at') is not None: timestamp(value['observed_at'])
    if value.get('source') is not None: identifier(value['source'])
    if value.get('model') is not None: model_name(value['model'])


def validate_policy(value):
    if not isinstance(value,dict) or set(value)-{'threshold_tokens','warning_margin_tokens','timezone','budgets'}: raise LedgerError('ledger_policy_invalid')
    for name in ('threshold_tokens','warning_margin_tokens'):
        if value.get(name) is not None and count(value[name]) is None: raise LedgerError('ledger_policy_invalid')
    if value.get('threshold_tokens') == 0: raise LedgerError('ledger_policy_invalid')
    try: ZoneInfo(value.get('timezone','Asia/Shanghai'))
    except (ValueError,KeyError,TypeError): raise LedgerError('ledger_policy_invalid') from None
    budgets=value.get('budgets',[])
    if not isinstance(budgets,list) or len(budgets)>10: raise LedgerError('ledger_policy_invalid')
    keys=set()
    for b in budgets:
        if not isinstance(b,dict) or set(b)!={'period','currency','limit'} or b['period'] not in {'day','month'} or number(b['limit']) is None or not isinstance(b['currency'],str) or not re.fullmatch('[A-Z]{3}',b['currency']): raise LedgerError('ledger_budget_invalid')
        key=(b['period'],b['currency'])
        if key in keys: raise LedgerError('ledger_budget_invalid')
        keys.add(key)
    return value


def context(value, policy=None):
    value = value if isinstance(value, dict) else {}
    used, limit = count(value.get('tokens')), count(value.get('limit'))
    threshold = count((policy or {}).get('threshold_tokens'))
    margin = count((policy or {}).get('warning_margin_tokens'))
    distance = max(0, threshold-used) if used is not None and threshold and (not limit or threshold <= limit) else None
    return {**value, 'tokens': used, 'limit': limit if limit else None,
            'used_percent': round(used/limit*100, 2) if used is not None and limit else None,
            'remaining': max(0, limit-used) if used is not None and limit else None,
            'threshold_tokens': threshold, 'distance_to_threshold': distance,
            'warning': bool(distance is not None and margin is not None and distance <= margin and value.get('stale') is False),
            'warning_margin_tokens': margin, 'automatic_switch_enabled': False}


def stable_prefix(instructions, tools, key):
    """Offline helper only; caller explicitly supplies stable sections and HMAC key.

    Never rewrites memory/context policies or inserts volatile time/session fields.
    Tool list order is preserved. No text or plain-text hash is stored in ledger.
    """
    if not isinstance(instructions, str) or not isinstance(tools, list) or not isinstance(key, bytes) or len(key) < 32: raise LedgerError('ledger_prefix_invalid')
    prefix = json.dumps({'instructions': instructions, 'tools': tools}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return prefix, hmac.new(key, prefix.encode(), hashlib.sha256).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', value): raise LedgerError('ledger_id_invalid')
    return value


def model_name(value):
    # OpenAI-compatible gateways may return aliases such as [Pro按量]model.
    if not isinstance(value,str) or not 1<=len(value)<=160 or len(value.encode())>640 or any(ord(c)<32 or ord(c)==127 for c in value): raise LedgerError('ledger_model_invalid')
    return value


def timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 40: raise ValueError()
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None: raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError: raise LedgerError('ledger_time_invalid') from None


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        with closing(self.connect()) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger (mode TEXT NOT NULL, provider_id TEXT NOT NULL, request_id TEXT NOT NULL, session_id TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(mode,provider_id,request_id))')
            conn.execute('CREATE INDEX IF NOT EXISTS usage_ledger_session_time ON usage_ledger(session_id,mode,at)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_events (event_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,mode TEXT NOT NULL,at TEXT NOT NULL,payload TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_settings (mode TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_sequence(id INTEGER PRIMARY KEY AUTOINCREMENT,mode TEXT NOT NULL,provider_id TEXT NOT NULL,request_id TEXT NOT NULL,UNIQUE(mode,provider_id,request_id))')
            conn.execute('INSERT OR IGNORE INTO usage_ledger_sequence(mode,provider_id,request_id) SELECT mode,provider_id,request_id FROM usage_ledger ORDER BY rowid')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_attempts(mode TEXT,provider_id TEXT,request_id TEXT,session_id TEXT,at TEXT,state TEXT,run_id TEXT,updated_at TEXT,PRIMARY KEY(mode,provider_id,request_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_issues(issue_id TEXT PRIMARY KEY,mode TEXT,provider_id TEXT,request_id TEXT,session_id TEXT,at TEXT,reason TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_runs(run_id TEXT PRIMARY KEY,mode TEXT,provider_id TEXT,started_at TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_retired(session_id TEXT PRIMARY KEY,at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_retired_modes(session_id TEXT,mode TEXT,PRIMARY KEY(session_id,mode))')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_removed(mode TEXT,provider_id TEXT,request_id TEXT,PRIMARY KEY(mode,provider_id,request_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_storage(mode TEXT PRIMARY KEY,raw_before TEXT,records_before TEXT,removed INTEGER NOT NULL DEFAULT 0)')
            conn.execute('CREATE TABLE IF NOT EXISTS usage_ledger_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            import secrets
            conn.execute("INSERT OR IGNORE INTO usage_ledger_meta VALUES ('cursor_key',?)",(secrets.token_hex(32),))
            conn.execute("INSERT OR IGNORE INTO usage_ledger_meta VALUES ('revision','0')")
            conn.commit()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=3); conn.row_factory = sqlite3.Row
        return conn

    def blocked(self,conn,mode,sid,at):
        if conn.execute('SELECT 1 FROM usage_ledger_retired WHERE session_id=?',(sid,)).fetchone():return True
        row=conn.execute('SELECT records_before FROM usage_ledger_storage WHERE mode=?',(mode,)).fetchone()
        return bool(row and row[0] and timestamp(at)<row[0])

    def has_record(self,mode,provider,request):
        with closing(self.connect()) as conn:
            return bool(conn.execute('SELECT 1 FROM usage_ledger WHERE mode=? AND provider_id=? AND request_id=?',(mode,provider,request)).fetchone())

    def iter_records(self,mode,sid=None,*,model=None,turn=None,conn=None):
        if conn is None:
            with closing(self.connect()) as opened:
                opened.execute('BEGIN');yield from self.iter_records(mode,sid,model=model,turn=turn,conn=opened)
            return
        if mode not in ('api','codex'):raise LedgerError('ledger_mode_invalid')
        where=['mode=?'];args=[mode]
        if sid is not None:where.append('session_id=?');args.append(identifier(sid))
        if model is not None:model_name(model);where.append("json_extract(payload,'$.model')=?");args.append(model)
        if turn is not None:where.append("json_extract(payload,'$.turn_id')=?");args.append(identifier(turn))
        cursor=conn.execute('SELECT payload FROM usage_ledger WHERE '+' AND '.join(where)+' ORDER BY at,rowid',args)
        while batch:=cursor.fetchmany(256):
            for row in batch:yield json.loads(row[0])

    def iter_events(self,mode,sid=None,*,conn=None):
        if conn is None:
            with closing(self.connect()) as opened:yield from self.iter_events(mode,sid,conn=opened)
            return
        cursor=conn.execute('SELECT payload FROM usage_ledger_events WHERE mode=?'+(' AND session_id=?' if sid is not None else '')+' ORDER BY at,event_id',(mode,sid) if sid is not None else (mode,))
        while batch:=cursor.fetchmany(256):
            for row in batch:yield json.loads(row[0])

    def retention(self,mode,*,conn=None):
        if conn is None:
            with closing(self.connect()) as opened:return self.retention(mode,conn=opened)
        row=conn.execute('SELECT * FROM usage_ledger_storage WHERE mode=?',(mode,)).fetchone()
        retired=conn.execute('SELECT COUNT(*) FROM usage_ledger_retired_modes WHERE mode=?',(mode,)).fetchone()[0]
        return {'raw_before':row['raw_before'] if row else None,'records_before':row['records_before'] if row else None,
                'records_removed':bool(retired or row and row['removed']),'retired_sessions':retired,'default':'keep_all','automatic_cleanup':False}

    def append(self, value):
        if not isinstance(value, dict) or set(value)-{'mode','provider_id','request_id','turn_id','session_id','at','model','requested_model','model_source','dialect','raw_usage','usage_baseline','accounting_basis','usage_complete','status','latency_ms','http_status','retry_count','price','context','policy','prefix_fingerprint','cache_policy','config_revision'}: raise LedgerError('ledger_record_invalid')
        for name in ('provider_id', 'request_id', 'turn_id', 'session_id'): identifier(value.get(name))
        model_name(value.get('model'))
        if value.get('requested_model') is not None: model_name(value['requested_model'])
        if value.get('model_source') is not None and value['model_source'] not in {'request','provider'}: raise LedgerError('ledger_record_invalid')
        mode = value.get('mode'); dialect = value.get('dialect')
        if mode not in ('api', 'codex') or (mode == 'codex') != (dialect == 'codex'): raise LedgerError('ledger_mode_invalid')
        if value.get('usage_baseline') is not None and (mode != 'codex' or not isinstance(value['usage_baseline'],dict)): raise LedgerError('ledger_record_invalid')
        if value.get('accounting_basis') is not None and (mode != 'codex' or value['accounting_basis'] not in {'thread_counter_delta','unavailable'}): raise LedgerError('ledger_record_invalid')
        if type(value.get('usage_complete')) is not bool or value.get('status') not in {'completed','error','timeout','rate_limited','interrupted','unknown'}: raise LedgerError('ledger_record_invalid')
        for name in ('latency_ms', 'retry_count'):
            if value.get(name) is not None and count(value[name]) is None: raise LedgerError('ledger_record_invalid')
        status = value.get('http_status')
        if status is not None and (type(status) is not int or not 100 <= status <= 599): raise LedgerError('ledger_record_invalid')
        fingerprint = value.get('prefix_fingerprint')
        if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint)): raise LedgerError('ledger_record_invalid')
        normalized = normalize(value.get('raw_usage'), dialect, usage_baseline=value.get('usage_baseline'))
        validate_context(value.get('context'))
        if value.get('policy') is not None: validate_policy(value['policy'])
        for name in ('cache_policy','config_revision'):
            if value.get(name) is not None: identifier(value[name])
        money = estimate(normalized, value.get('price'))
        if not value['usage_complete'] or normalized['issues']: money.update(amount=None,savings=None,basis='unavailable')
        payload = {**value, 'at': timestamp(value.get('at')), 'normalized': normalized, 'cost': money}
        # Raw usage is retained once, separately from the normalized projection.
        payload['normalized'] = {k: v for k, v in normalized.items() if k != 'raw'}
        encoded = raw_json(payload)
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.blocked(conn,mode,value['session_id'],payload['at']) or conn.execute('SELECT 1 FROM usage_ledger_removed WHERE mode=? AND provider_id=? AND request_id=?',(mode,value['provider_id'],value['request_id'])).fetchone():return False
            previous = conn.execute('SELECT payload FROM usage_ledger WHERE mode=? AND provider_id=? AND request_id=?', (mode,value['provider_id'],value['request_id'])).fetchone()
            if previous:
                if previous[0] != encoded:
                    import hashlib
                    old=json.loads(previous[0])
                    if old.get('_retention',{}).get('original_digest')!=hashlib.sha256(encoded.encode()).hexdigest():raise LedgerError('ledger_duplicate_conflict')
                return False
            retention=conn.execute('SELECT raw_before FROM usage_ledger_storage WHERE mode=?',(mode,)).fetchone()
            if retention and retention[0] and payload['at']<retention[0]:
                import hashlib
                payload['raw_usage']=None
                if 'usage_baseline' in payload:payload['usage_baseline']=None
                payload['_retention']={'raw_removed':True,'original_digest':hashlib.sha256(encoded.encode()).hexdigest()}
                encoded=raw_json(payload)
            conn.execute('INSERT INTO usage_ledger VALUES (?,?,?,?,?,?)', (mode,value['provider_id'],value['request_id'],value['session_id'],payload['at'],encoded))
            conn.execute('INSERT OR IGNORE INTO usage_ledger_sequence(mode,provider_id,request_id) VALUES (?,?,?)',(mode,value['provider_id'],value['request_id']))
            conn.execute("UPDATE usage_ledger_attempts SET state='recorded',updated_at=? WHERE mode=? AND provider_id=? AND request_id=?",(datetime.now(timezone.utc).isoformat(),mode,value['provider_id'],value['request_id']))
            conn.execute("DELETE FROM usage_ledger_issues WHERE mode=? AND provider_id=? AND request_id=? AND reason='write_failed'",(mode,value['provider_id'],value['request_id']))
            if value['usage_complete'] and not normalized['issues']:
                conn.execute("DELETE FROM usage_ledger_issues WHERE mode=? AND provider_id=? AND request_id=? AND reason='invalid_usage'",(mode,value['provider_id'],value['request_id']))
            conn.commit()
        return True

    def event(self, value, *, continuation_receipt=None, manual_compaction_receipt=None):
        allowed = {'event_id','session_id','mode','at','kind','status','before_tokens','after_tokens','memory_preserved','target_session_id','snapshot','source'}
        if not isinstance(value, dict) or set(value)-allowed or value.get('kind') not in {'compaction','continuation','model_switch','message_counts','context'} or value.get('mode') not in {'api','codex'} or value.get('status') not in {'completed','failed','unknown','observed'}: raise LedgerError('ledger_event_invalid')
        for key in ('event_id', 'session_id'): identifier(value.get(key))
        for key in ('before_tokens','after_tokens'):
            if value.get(key) is not None and count(value[key]) is None: raise LedgerError('ledger_event_invalid')
        if value.get('memory_preserved') is not None and type(value['memory_preserved']) is not bool: raise LedgerError('ledger_event_invalid')
        if value.get('memory_preserved') is not None and value.get('source') != 'independent_memory_verification': raise LedgerError('ledger_memory_unverified')
        if value.get('target_session_id') is not None: identifier(value['target_session_id'])
        if value.get('source') is not None: identifier(value['source'])
        snapshot=value.get('snapshot')
        if value['kind']=='context': validate_context(snapshot)
        elif value['kind']=='message_counts':
            if not isinstance(snapshot,dict) or set(snapshot)-{'total','visible','folded','archived','turns'} or any(v is not None and count(v) is None for v in snapshot.values()): raise LedgerError('ledger_counts_invalid')
            if count(snapshot.get('total')) is not None and all(count(snapshot.get(k)) is not None for k in ('visible','folded','archived')) and add(*(snapshot[k] for k in ('visible','folded','archived'))) != snapshot['total']: raise LedgerError('ledger_counts_invalid')
        elif value['kind']=='model_switch':
            if not isinstance(snapshot,dict) or set(snapshot)-{'model','context_limit','cache_policy','price'}: raise LedgerError('ledger_event_invalid')
            model_name(snapshot.get('model'))
            if snapshot.get('context_limit') is not None and count(snapshot['context_limit']) is None: raise LedgerError('ledger_event_invalid')
        elif value['kind']=='continuation' and snapshot is not None:
            if (not isinstance(snapshot,dict) or set(snapshot)!={'stage','success','input_message_id','reply_message_id'}
                    or snapshot['stage'] not in {'target_created','message_received','reply_completed','reply_failed','content_unverified'}
                    or value.get('source')!='canonical_continuation_receipt' or not value.get('target_session_id')): raise LedgerError('ledger_event_invalid')
            stage=snapshot['stage']
            expected_status='completed' if stage=='reply_completed' else 'failed' if stage=='reply_failed' else 'unknown' if stage=='content_unverified' else 'observed'
            expected_success=True if stage=='reply_completed' else False if stage=='reply_failed' else None
            if value['status']!=expected_status or snapshot['success'] is not expected_success: raise LedgerError('ledger_event_invalid')
            for key in ('input_message_id','reply_message_id'):
                if snapshot[key] is not None and (count(snapshot[key]) is None or snapshot[key]==0): raise LedgerError('ledger_event_invalid')
            if stage=='target_created' and snapshot['input_message_id'] is not None or stage!='target_created' and snapshot['input_message_id'] is None: raise LedgerError('ledger_event_invalid')
            if (stage=='reply_completed') != (snapshot['reply_message_id'] is not None): raise LedgerError('ledger_event_invalid')
        elif value['kind']=='compaction' and snapshot is not None:
            if (not isinstance(snapshot,dict) or set(snapshot)!={'request_id','stage','dispatched','after_usage_observed'}
                    or value.get('source')!='codex_manual_task' or value['mode']!='codex'
                    or type(snapshot['dispatched']) is not bool or type(snapshot['after_usage_observed']) is not bool):raise LedgerError('ledger_event_invalid')
            identifier(snapshot['request_id'])
            stage=snapshot['stage']
            if stage not in {'queued','preparing','dispatching','running','unknown','reconcile_queued','reconciling','completed','failed'}:raise LedgerError('ledger_event_invalid')
            expected='completed' if stage=='completed' else 'failed' if stage=='failed' else 'unknown' if stage=='unknown' else 'observed'
            if value['status']!=expected:raise LedgerError('ledger_event_invalid')
        elif snapshot is not None: raise LedgerError('ledger_event_invalid')
        payload = {**value,'at':timestamp(value.get('at'))}; encoded = raw_json(payload)
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.blocked(conn,value['mode'],value['session_id'],payload['at']) or value.get('target_session_id') and conn.execute('SELECT 1 FROM usage_ledger_retired WHERE session_id=?',(value['target_session_id'],)).fetchone():return False
            # A purge can race with a read-side reconciliation. Serialize the
            # receipt existence check and event write with purge_session, and
            # bind to this registration epoch rather than a reused request ID.
            if continuation_receipt is not None:
                request_id,created_at=continuation_receipt
                if value['kind']!='continuation' or not conn.execute('SELECT 1 FROM usage_ledger_continuations WHERE request_id=? AND created_at=? AND source_id=? AND target_id=? AND mode=?',
                    (request_id,created_at,value['session_id'],value.get('target_session_id'),value['mode'])).fetchone(): return False
            if manual_compaction_receipt is not None:
                request_id,created_at=manual_compaction_receipt
                if value['kind']!='compaction' or not conn.execute('SELECT 1 FROM usage_ledger_manual_compactions WHERE request_id=? AND created_at=? AND session_id=?',
                    (request_id,created_at,value['session_id'])).fetchone():return False
            old = conn.execute('SELECT payload FROM usage_ledger_events WHERE event_id=?',(value['event_id'],)).fetchone()
            if old:
                if old[0] != encoded: raise LedgerError('ledger_duplicate_conflict')
                return False
            conn.execute('INSERT INTO usage_ledger_events VALUES (?,?,?,?,?)',(value['event_id'],value['session_id'],value['mode'],payload['at'],encoded)); conn.commit()
        return True

    def records(self, mode, sid=None, *, model=None):
        if mode not in ('api', 'codex'): raise LedgerError('ledger_mode_invalid')
        where, params = ['mode=?'], [mode]
        if sid is not None: where.append('session_id=?'); params.append(identifier(sid))
        if model is not None: model_name(model)
        with closing(self.connect()) as conn:
            rows = conn.execute('SELECT payload FROM usage_ledger WHERE '+' AND '.join(where)+' ORDER BY at,rowid LIMIT 50001',params).fetchall()
        if len(rows)>50000: raise LedgerError('ledger_query_too_large')
        values = [json.loads(r[0]) for r in rows]
        return [v for v in values if model is None or v['model'] == model]

    def events(self, mode, sid=None):
        with closing(self.connect()) as conn:
            rows = conn.execute('SELECT payload FROM usage_ledger_events WHERE mode=?'+(' AND session_id=?' if sid else '')+' ORDER BY at,event_id', (mode,sid) if sid else (mode,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def settings(self,mode):
        if mode not in ('api','codex'): raise LedgerError('ledger_mode_invalid')
        with closing(self.connect()) as conn:
            row=conn.execute('SELECT revision,payload FROM usage_ledger_settings WHERE mode=?',(mode,)).fetchone()
        return {'ok':True,'revision':row[0] if row else 0,'mode':mode,'policy':json.loads(row[1]) if row else {'threshold_tokens':None,'warning_margin_tokens':None,'timezone':'Asia/Shanghai','budgets':[]}}

    def save_settings(self,mode,revision,policy):
        if mode not in ('api','codex') or count(revision) is None: raise LedgerError('ledger_policy_invalid')
        validate_policy(policy)
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE'); old=conn.execute('SELECT revision FROM usage_ledger_settings WHERE mode=?',(mode,)).fetchone()
            if revision != (old[0] if old else 0): raise LedgerError('ledger_revision_conflict')
            conn.execute('INSERT OR REPLACE INTO usage_ledger_settings VALUES (?,?,?)',(mode,revision+1,raw_json(policy)));conn.commit()
        return self.settings(mode)

    def purge_session(self, sid, *, retire=False, mode=None):
        identifier(sid)
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            modes={r[0] for r in conn.execute('SELECT mode FROM usage_ledger WHERE session_id=? UNION SELECT mode FROM usage_ledger_attempts WHERE session_id=?',(sid,sid))}
            if mode in {'api','codex'}:modes.add(mode)
            for m in modes:
                conn.execute('INSERT INTO usage_ledger_storage(mode,removed) VALUES (?,1) ON CONFLICT(mode) DO UPDATE SET removed=1',(m,))
                if retire:conn.execute('INSERT OR IGNORE INTO usage_ledger_retired_modes VALUES (?,?)',(sid,m))
            if retire:conn.execute('INSERT OR IGNORE INTO usage_ledger_retired VALUES (?,?)',(sid,datetime.now(timezone.utc).isoformat()))
            conn.execute('INSERT OR IGNORE INTO usage_ledger_removed SELECT mode,provider_id,request_id FROM usage_ledger WHERE session_id=?',(sid,))
            conn.execute('INSERT OR IGNORE INTO usage_ledger_removed SELECT mode,provider_id,request_id FROM usage_ledger_attempts WHERE session_id=?',(sid,))
            conn.execute('DELETE FROM usage_ledger_attempts WHERE session_id=?',(sid,));conn.execute('DELETE FROM usage_ledger_issues WHERE session_id=?',(sid,))
            conn.execute('DELETE FROM usage_ledger WHERE session_id=?',(sid,)); conn.execute('DELETE FROM usage_ledger_events WHERE session_id=? OR json_extract(payload,\'$.target_session_id\')=?',(sid,sid))
            conn.execute("UPDATE usage_ledger_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_ledger_continuations'").fetchone():
                conn.execute('DELETE FROM usage_ledger_continuations WHERE source_id=? OR target_id=?',(sid,sid))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_ledger_continuation_creations'").fetchone():
                conn.execute('DELETE FROM usage_ledger_continuation_creations WHERE source_id=? OR target_id=?',(sid,sid))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_ledger_manual_compactions'").fetchone():
                conn.execute('INSERT OR REPLACE INTO usage_ledger_manual_purges VALUES (?,?)',(sid,datetime.now(timezone.utc).isoformat()))
                conn.execute('DELETE FROM usage_ledger_manual_compactions WHERE session_id=?',(sid,))
            conn.commit()


def metric_sum(records, field):
    values = [r['normalized']['metrics'][field] for r in records]
    known = [v for v in values if v is not None]
    # Empty history is not proof that the provider reported zero tokens.
    return {'value': add(*known) if known else None, 'reported_requests': len(known), 'requests': len(records),
            'complete': bool(values) and add(*known) is not None and len(known) == len(values) and all(r['usage_complete'] for r in records)}


def summarize(records):
    codex = bool(records) and all(r['mode']=='codex' for r in records)
    metrics = {key: metric_sum(records,key) for key in METRICS}
    pair = [r for r in records if r['normalized']['cache_hit_rate'] is not None]
    denom = sum(r['normalized']['metrics']['input'] for r in pair)
    rate = sum(r['normalized']['metrics']['cache_read'] for r in pair)/denom if denom else None
    currencies = sorted({r['cost']['currency'] for r in records if r['cost']['currency']})
    costs = []
    for currency in currencies:
        bucket = [r for r in records if r['cost']['currency'] == currency]
        known = [r for r in bucket if r['cost']['amount'] is not None]
        savings = [r for r in bucket if r['cost']['savings'] is not None]
        with localcontext() as precision:
            precision.prec=60
            costs.append({'currency':currency,'amount':str(sum(Decimal(r['cost']['amount']) for r in known)) if known else None,
                          'savings':str(sum(Decimal(r['cost']['savings']) for r in savings)) if savings else None,
                          'complete':bool(known) and len(known)==len(bucket) and all(r['cost']['currency'] is not None for r in records),'priced_requests':len(known),'requests':len(bucket),'basis':'estimated_price_snapshot','scope':'token_only'})
    latency = [r['latency_ms'] for r in records if r.get('latency_ms') is not None]
    known_status = [r for r in records if r['status']!='unknown']
    return {'requests':len(records),'observed_turns':len({(r['session_id'],r['turn_id']) for r in records}),
            'metrics':metrics,'cache_hit_rate':rate,'cache_rate_requests':len(pair),'cache_rate_complete':bool(pair) and len(pair)==len(records) and all(r['usage_complete'] for r in records),'costs':costs,
            'latency_ms':sum(latency)/len(latency) if latency else None,'latency_requests':len(latency),
            'error_rate':sum(r['status'] in {'error','timeout','rate_limited'} for r in known_status)/len(known_status) if known_status else None,
            'status_requests':len(known_status),'status_complete':bool(records) and len(known_status)==len(records),
            'timeouts':sum(r['status']=='timeout' for r in known_status) if known_status and not codex else None,
            'rate_limited':sum(r['status']=='rate_limited' for r in known_status) if known_status and not codex else None,
            'retries':sum(r['retry_count'] for r in records) if records and all(r.get('retry_count') is not None for r in records) else None}


def build_view(ledger,mode,sid,**kwargs):
    from .usage_ledger_report import build_report
    return build_report(ledger,mode,sid,**kwargs)


def export_records(records, format):
    if format=='json': return json.dumps({'contract_version':1,'records':records},ensure_ascii=False,allow_nan=False,indent=2)
    if format!='csv': raise LedgerError('ledger_export_invalid')
    output=io.StringIO();fields=['mode','provider_id','session_id','request_id','turn_id','at','model','requested_model','model_source','accounting_basis',*METRICS,'usage_complete','status','latency_ms','http_status','retry_count','amount','currency','cost_basis','raw_removed','retained_records_before','raw_usage_json','usage_baseline_json']
    writer=csv.DictWriter(output,fieldnames=fields);writer.writeheader()
    for r in records:
        row={k:r.get(k) for k in fields if k in r};row.update(r['normalized']['metrics']);row.update(amount=r['cost']['amount'],currency=r['cost']['currency'],cost_basis=r['cost']['basis'],raw_removed=bool(r.get('_retention',{}).get('raw_removed')),retained_records_before=(r.get('_export_scope') or {}).get('records_before'),raw_usage_json=json.dumps(r.get('raw_usage'),ensure_ascii=False),usage_baseline_json=json.dumps(r.get('usage_baseline'),ensure_ascii=False))
        # Null CSV cells stay blank; numeric zero stays 0. Neutralize formulas.
        row={k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()}
        writer.writerow(row)
    return output.getvalue()
