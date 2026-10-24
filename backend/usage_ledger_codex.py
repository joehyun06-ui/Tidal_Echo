"""Default-off, passive Codex 0.160 turn accounting. Never sends RPC or reads text.

Native notifications may beat the turn/start ACK. Stage bounded observations,
then correlate exact durable thread/turn/canonical message identities before any
ledger write. Thread totals are differenced only across a continuously observed,
verified terminal boundary. A restart, gap, compaction or missing start removes
that baseline. The last response is retained for context, never summed as a turn.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone

from .usage_ledger import count, identifier, normalize, raw_json
from .usage_ledger_journal import CollectionJournal

NOTIFICATIONS = frozenset({'turn/started','turn/completed','thread/tokenUsage/updated','item/started','item/completed'})


def digest(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def last_usage(raw):
    value = raw.get('last') if isinstance(raw,dict) else None
    return value if isinstance(value,dict) else {}


class CodexCollector:
    def __init__(self, ledger, *, relay_db, store_path, provider_id, max_pending=32, exclusive_threads=False):
        identifier(provider_id)
        if type(max_pending) is not int or not 1 <= max_pending <= 128: raise ValueError('ledger_pending_invalid')
        if type(exclusive_threads) is not bool: raise ValueError('ledger_thread_authority_invalid')
        self.ledger, self.relay_db, self.store_path = ledger, str(relay_db), str(store_path)
        self.provider_id, self.max_pending = provider_id, max_pending
        # Cross-turn counters cannot be assigned to a turn when other clients may write it.
        # The composition owner must explicitly attest its private native threads.
        self.exclusive_threads = exclusive_threads
        self._lock = threading.RLock(); self._writer = threading.Lock()
        self._pending = OrderedDict(); self._finished = OrderedDict(); self._heads = {}
        self._epoch = None; self._sequence = 0; self._instance = uuid.uuid4().hex
        self.captured = self.dropped = self.unmatched = self.late = self.evicted = 0
        self.journal_failed=0;self._issues=OrderedDict();self._bindings={}
        try:self.journal=CollectionJournal(ledger,'codex',provider_id)
        except Exception:self.journal=None;self.journal_failed=1

    def _issue(self,reason,key):
        identity=(reason,*key)
        if identity not in self._issues:
            if len(self._issues)>=256:self._issues.popitem(last=False);self.journal_failed+=1
            self._issues[identity]={'session_id':self._bindings.get(key),'request_id':'codex-'+digest(self.provider_id,*key),'at':datetime.now(timezone.utc).isoformat()}

    def health(self):
        with self._lock:
            return {'captured':self.captured,'dropped':self.dropped,'unmatched':self.unmatched,
                    'late':self.late,'evicted':self.evicted,'pending':len(self._pending),
                    'journal_failed':self.journal_failed,'complete':not (self.dropped or self.unmatched or self.late or self.evicted or self.journal_failed)}

    def _continuity(self, epoch):
        if epoch != self._epoch:
            for key in set(self._heads.values())|set(self._pending):self._issue('epoch_changed',key)
            self._heads.clear()
            for state in self._pending.values(): state['invalid'] = True; state['revision'] += 1
            self._epoch = epoch

    def receive(self, method, params, *, epoch):
        if method not in NOTIFICATIONS or not isinstance(params,dict): return
        thread = params.get('threadId'); native = params.get('turn') if method in {'turn/started','turn/completed'} else None
        turn = native.get('id') if isinstance(native,dict) else params.get('turnId')
        try: identifier(thread); identifier(turn)
        except ValueError: return
        compact = method in {'item/started','item/completed'}
        item = params.get('item')
        if compact and (not isinstance(item,dict) or item.get('type') != 'contextCompaction'): return
        if compact:
            try: identifier(item.get('id'))
            except ValueError: return
        usage = None
        if method == 'thread/tokenUsage/updated':
            try:
                if not isinstance(params.get('tokenUsage'),dict): raise ValueError()
                usage = json.loads(raw_json(params['tokenUsage']))
            except (ValueError,TypeError,RecursionError):
                with self._lock:
                    self._issue('invalid_usage',(thread,turn))
                    self.dropped += 1; self._heads.pop(thread,None)
                    if (thread,turn) in self._pending: self._pending[thread,turn]['invalid'] = True
                return
        now = datetime.now(timezone.utc).isoformat(); key = (thread,turn)
        with self._lock:
            self._continuity(epoch)
            if key in self._finished:
                old = self._finished[key]
                if method == 'thread/tokenUsage/updated' and usage != old.get('raw'):
                    self._issue('late_event',key)
                    self.late += 1; self._heads.pop(thread,None)
                return
            state = self._pending.get(key)
            if state is None:
                overlap = False
                for other in self._pending.values():
                    if other['thread']==thread and not other['terminal']:
                        other['invalid'] = True; other['revision'] += 1; overlap = True
                while len(self._pending) >= self.max_pending:
                    removed,_ = self._pending.popitem(last=False);self._issue('pending_evicted',removed); self._heads.pop(removed[0],None); self.evicted += 1
                self._sequence += 1
                state = {'thread':thread,'turn':turn,'first':self._sequence,'revision':0,'started':False,
                         'terminal':None,'raw':None,'observed_at':None,'at':now,'previous':None,
                         'invalid':epoch is None or overlap,'compactions':[]}
                self._pending[key] = state
            if method == 'turn/started':
                if state['started'] or state['terminal']: return
                if state['raw'] is not None: state['invalid'] = True
                state['started'] = True
                state['previous'] = self._heads.get(thread)
                self._heads.pop(thread,None)
            elif method == 'thread/tokenUsage/updated':
                if state['raw'] == usage: return
                if state['terminal']:
                    self._issue('late_event',key)
                    state['invalid'] = True; state['revision'] += 1; self.late += 1; self._heads.pop(thread,None)
                    return
                previous = (state['raw'] or {}).get('total',{})
                current = usage.get('total',{})
                fields = ('inputTokens','outputTokens','reasoningOutputTokens','totalTokens')
                if isinstance(previous,dict) and isinstance(current,dict) and any(count(previous.get(k)) is not None and count(current.get(k)) is not None and current[k] < previous[k] for k in fields): state['invalid'] = True
                state['raw'], state['observed_at'] = usage, now
            elif compact:
                state['invalid'] = True; self._heads.pop(thread,None)
                event = {'id':item['id'],'status':'completed' if method=='item/completed' else 'unknown','at':now,
                         'before_tokens':count(last_usage(state['raw']).get('totalTokens'))}
                old = next((c for c in state['compactions'] if c['id']==item['id']),None)
                if old:
                    if old['status']=='completed' or event['status']=='unknown': return
                    old['status']='completed';old['at']=now
                elif len(state['compactions']) < 16: state['compactions'].append(event)
                else: self.evicted += 1
            else:
                if state['terminal']: return
                status = native.get('status') if isinstance(native,dict) else None
                state['terminal'] = status if status in {'completed','failed','interrupted'} else 'unknown'
                state['ended_at'] = now
                self._heads[thread] = key
            state['revision'] += 1
            # Thread heads are bounded independently of pending turns.
            while len(self._heads) > 256: self._heads.pop(next(iter(self._heads)))

    def _binding(self, state):
        # Read only routing identities, never prompts, input digests or chat text.
        with closing(sqlite3.connect(self.store_path,timeout=1)) as conn:
            conn.row_factory = sqlite3.Row
            created=',j.created_at AS native_created_at' if 'created_at' in {r[1] for r in conn.execute('PRAGMA table_info(codex_generation_jobs)')} else ',NULL AS native_created_at'
            row = conn.execute('''SELECT j.id,j.api_session,j.canonical_message_id,j.turn_id,j.thread_id,j.status,s.model'''+created+'''
                FROM codex_generation_jobs j JOIN codex_sessions s ON s.api_session=j.api_session
                WHERE j.thread_id=? AND j.turn_id=? AND s.thread_id=j.thread_id ORDER BY j.id DESC LIMIT 1''',
                (state['thread'],state['turn'])).fetchone()
            if row is None: return None
            job = dict(row)
            latest = conn.execute('SELECT max(id) FROM codex_generation_jobs WHERE thread_id=?',(state['thread'],)).fetchone()[0]
            prior = conn.execute('SELECT turn_id FROM codex_generation_jobs WHERE thread_id=? AND id<? ORDER BY id DESC LIMIT 1',(state['thread'],job['id'])).fetchone()
            job['previous_turn'] = prior[0] if prior else None
        with closing(sqlite3.connect(self.relay_db,timeout=1)) as conn:
            exists = conn.execute("SELECT 1 FROM messages WHERE id=? AND direction='in' AND kind IN ('user','voice') AND json_extract(meta,'$.api_session')=?",(job['canonical_message_id'],job['api_session'])).fetchone()
            newest = conn.execute("SELECT max(id) FROM messages WHERE direction='in' AND kind IN ('user','voice') AND json_extract(meta,'$.api_session')=?",(job['api_session'],)).fetchone()[0]
        if not exists: return None
        job['current'] = latest == job['id'] and newest == job['canonical_message_id']
        return job

    def reconcile(self, *, epoch):
        # A slow disk does not block the native notification reader or a reply.
        if not self._writer.acquire(blocking=False): return
        try:
            with self._lock:
                self._continuity(epoch)
                states = [json.loads(json.dumps(s)) for s in self._pending.values()]
            for state in states:
                try: self._reconcile(state)
                except Exception:
                    with self._lock:self.dropped += 1;self._issue('collector_failure',(state['thread'],state['turn']))
            with self._lock:
                issues=list(self._issues.items());self._issues.clear()
            for (reason,*_),fields in issues:
                try:self.journal.issue(reason,**fields)
                except Exception:self.journal_failed+=1
        finally: self._writer.release()

    def _reconcile(self, state):
        key = (state['thread'],state['turn']); binding = self._binding(state)
        if binding is None: return  # ACK may still be in flight. Never guess the turn.
        with self._lock:
            actual = self._pending.get(key)
            if actual is None or actual['revision'] != state['revision']: return
            previous_key = tuple(state['previous']) if state['previous'] else None
            previous = self._finished.get(previous_key)
        sid = binding['api_session']; rid = 'codex-'+digest(self.provider_id,*key)
        at=binding.get('native_created_at') or state['at']
        with closing(self.ledger.connect()) as conn:
            blocked=self.ledger.blocked(conn,'codex',sid,at) or conn.execute('SELECT 1 FROM usage_ledger_removed WHERE mode=? AND provider_id=? AND request_id=?',('codex',self.provider_id,rid)).fetchone()
        if blocked:
            with self._lock:self._pending.pop(key,None);self._heads.pop(state['thread'],None)
            return
        with self._lock:
            self._bindings[key]=sid
            while len(self._bindings)>256:self._bindings.pop(next(iter(self._bindings)))
        try:self.journal.start({'mode':'codex','provider_id':self.provider_id,'request_id':rid,'session_id':sid,'at':at})
        except Exception:self.journal_failed+=1
        raw = state['raw']; last = last_usage(raw)
        context = {'tokens':count(last.get('totalTokens')),'limit':count((raw or {}).get('modelContextWindow')),
                   'source':'codex_app_server','stale':False,'observed_at':state['observed_at'],'model':binding['model']}
        if state['started'] and not state['observed_at']:
            context.update(stale=True,observed_at=state['at'],source='codex_turn_started')
        newest_compaction = state['compactions'][-1]['at'] if state['compactions'] else None
        if newest_compaction and (not state['observed_at'] or newest_compaction>=state['observed_at']):
            context.update(tokens=None,stale=True,observed_at=newest_compaction,source='codex_compaction')
        if binding['current'] and context['observed_at']:
            self.ledger.event({'event_id':'context-'+digest(self._instance,*key,context['observed_at']),
                'session_id':sid,'mode':'codex','at':context['observed_at'],'kind':'context','status':'observed','snapshot':context,'source':'codex_app_server'})
        # The lifecycle record is immutable; finalize its post-observation once.
        for compact in state['compactions'] if state['terminal'] else []:
            after = context['tokens'] if state['observed_at'] and state['observed_at']>compact['at'] else None
            self.ledger.event({'event_id':'compact-'+digest(self._instance,*key,compact['id'],compact['status']),
                'session_id':sid,'mode':'codex','at':compact['at'],'kind':'compaction','status':compact['status'],
                'before_tokens':compact['before_tokens'],'after_tokens':after,'memory_preserved':None,'source':'codex_app_server'})
        terminal = state['terminal']
        if terminal is None: return
        baseline = None
        if (self.exclusive_threads and state['started'] and not state['invalid'] and previous and previous.get('checkpoint')
                and previous_key[1] == binding['previous_turn']
                and previous['model']==binding['model'] and previous['session_id']==sid):
            baseline = previous['raw'].get('total')
        if not isinstance((raw or {}).get('total'),dict): baseline = None
        normalized = normalize(raw,'codex',usage_baseline=baseline)
        complete = baseline is not None and all(normalized['metrics'][k] is not None for k in ('input','output','total')) and not normalized['issues'] and terminal != 'unknown'
        status = {'completed':'completed','failed':'error','interrupted':'interrupted','unknown':'unknown'}[terminal]
        payload = {'mode':'codex','provider_id':self.provider_id,'request_id':rid,'turn_id':state['turn'],
                   'session_id':sid,'at':at,'model':binding['model'],'requested_model':binding['model'],'model_source':'request',
                   'dialect':'codex','raw_usage':raw,'usage_baseline':baseline,
                   'accounting_basis':'thread_counter_delta' if baseline is not None else 'unavailable',
                   'usage_complete':complete,'status':status,'latency_ms':None,'http_status':None,'retry_count':None,'price':None}
        with self._lock:
            actual = self._pending.get(key)
            if actual is None or actual['revision'] != state['revision']: return
        # Deterministic native identity survives process restarts and event replay.
        with closing(self.ledger.connect()) as conn:
            exists = conn.execute('SELECT 1 FROM usage_ledger WHERE mode=? AND provider_id=? AND request_id=?',('codex',self.provider_id,rid)).fetchone()
        if not exists: self.ledger.append(payload)
        total = (raw or {}).get('total')
        checkpoint = (not exists and state['started'] and not state['invalid'] and terminal != 'unknown' and isinstance(total,dict)
                      and all(count(total.get(k)) is not None for k in ('inputTokens','outputTokens','totalTokens'))
                      and total['inputTokens']+total['outputTokens']==total['totalTokens'])
        with self._lock:
            self._pending.pop(key,None)
            self._finished[key] = {'raw':raw,'checkpoint':checkpoint,'session_id':sid,'model':binding['model']}
            while len(self._finished)>256: self._finished.popitem(last=False)
            if not exists: self.captured += 1
