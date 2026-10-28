"""One SQLite snapshot, bounded raw-row batches; no full response history list."""
from collections import defaultdict,deque
from contextlib import closing
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from zoneinfo import ZoneInfo
import re

from .usage_ledger import METRICS,LedgerError,context,count,model_name,number,timestamp
from .usage_ledger_journal import coverage
from .usage_ledger_rollover import preview as rollover_preview


class Summary:
    def __init__(self):
        self.n=0;self.turns=set();self.metrics={k:[0,0] for k in METRICS};self.complete=True
        self.cache_n=0;self.cache_in=0;self.cache_read=0;self.currency={};self.unknown_currency=False
        self.latency_n=0;self.latency=0;self.status_n=0;self.errors=0;self.timeouts=0;self.limited=0;self.retries=0;self.retry_known=True;self.codex=False

    def add(self,r):
        self.n+=1;self.turns.add((r['session_id'],r['turn_id']));self.codex=r['mode']=='codex'
        self.complete &= r['usage_complete']
        for k in METRICS:
            v=r['normalized']['metrics'][k]
            if v is not None:self.metrics[k][0]+=v;self.metrics[k][1]+=1
        if r['normalized']['cache_hit_rate'] is not None:
            self.cache_n+=1;self.cache_in+=r['normalized']['metrics']['input'];self.cache_read+=r['normalized']['metrics']['cache_read']
        c=r['cost'];currency=c['currency']
        if currency:
            b=self.currency.setdefault(currency,{'n':0,'known':0,'saved':0,'amount':Decimal(0),'savings':Decimal(0)})
            b['n']+=1
            with localcontext() as p:
                p.prec=60
                if c['amount'] is not None:b['known']+=1;b['amount']+=Decimal(c['amount'])
                if c['savings'] is not None:b['saved']+=1;b['savings']+=Decimal(c['savings'])
        else:self.unknown_currency=True
        if r.get('latency_ms') is not None:self.latency_n+=1;self.latency+=r['latency_ms']
        if r['status']!='unknown':
            self.status_n+=1;self.errors+=r['status'] in {'error','timeout','rate_limited'};self.timeouts+=r['status']=='timeout';self.limited+=r['status']=='rate_limited'
        if r.get('retry_count') is None:self.retry_known=False
        else:self.retries+=r['retry_count']

    def value(self):
        return {'requests':self.n,'observed_turns':len(self.turns),
            'metrics':{k:{'value':count(v) if n else None,'reported_requests':n,'requests':self.n,'complete':bool(self.n) and n==self.n and self.complete and count(v) is not None} for k,(v,n) in self.metrics.items()},
            'cache_hit_rate':self.cache_read/self.cache_in if self.cache_in else None,'cache_rate_requests':self.cache_n,'cache_rate_complete':bool(self.cache_n) and self.cache_n==self.n and self.complete,
            'costs':[{'currency':c,'amount':str(b['amount']) if b['known'] else None,'savings':str(b['savings']) if b['saved'] else None,'complete':bool(b['known']) and b['known']==b['n'] and not self.unknown_currency,'priced_requests':b['known'],'requests':b['n'],'basis':'estimated_price_snapshot','scope':'token_only'} for c,b in sorted(self.currency.items())],
            'latency_ms':self.latency/self.latency_n if self.latency_n else None,'latency_requests':self.latency_n,
            'error_rate':self.errors/self.status_n if self.status_n else None,'status_requests':self.status_n,'status_complete':bool(self.n) and self.status_n==self.n,
            'timeouts':self.timeouts if self.status_n and not self.codex else None,'rate_limited':self.limited if self.status_n and not self.codex else None,'retries':self.retries if self.n and self.retry_known else None}


def build_report(ledger,mode,sid,*,all_sessions=False,model=None,zone='Asia/Shanghai',now=None,budgets=None,message_source=None):
    if model is not None:model_name(model)
    settings=ledger.settings(mode);policy=settings['policy'];zone=policy.get('timezone',zone) if settings['revision'] else zone
    budgets=policy.get('budgets',[]) if budgets is None else budgets
    tz=ZoneInfo(zone);now=(now or datetime.now(timezone.utc)).astimezone(tz)
    total,daily,monthly=Summary(),Summary(),Summary();trends={};trends_complete=True;prior={};models=set()
    recent=deque(maxlen=100);changes=deque(maxlen=100);anomalies=deque(maxlen=100);history=deque(maxlen=100)
    observation=None;counts=None;last=None;latest_scope=None;invalidated=None
    with closing(ledger.connect()) as conn:
        conn.execute('BEGIN')
        for r in ledger.iter_records(mode,conn=conn):
            local=datetime.fromisoformat(r['at']).astimezone(tz)
            if local.date()==now.date():daily.add(r)
            if (local.year,local.month)==(now.year,now.month):monthly.add(r)
            if not all_sessions and r['session_id']!=sid:continue
            latest_scope=r;models.add(r['model'])
            if model is not None and r['model']!=model:continue
            last=r;total.add(r);recent.append({k:v for k,v in r.items() if k not in {'raw_usage','usage_baseline'}})
            key=(r['session_id'],r['provider_id']);old=prior.get(key)
            limit=(r.get('context') or {}).get('limit')
            if old:
                changed=[k for k in ('model','cache_policy','price','config_revision') if old.get(k)!=r.get(k)]
                if old['context_limit']!=limit:changed.append('context_limit')
                if changed:changes.append({'at':r['at'],'session_id':r['session_id'],'provider_id':r['provider_id'],'changed':changed,'before_model':old['model'],'model':r['model'],'price':r.get('price'),'context_limit':limit,'cache_policy':r.get('cache_policy')})
                a,b=old['rate'],r['normalized']['cache_hit_rate']
                if a is not None and b is not None and a-b>=.3:
                    reasons=['stable_prefix_changed'] if old.get('prefix_fingerprint') and r.get('prefix_fingerprint') and old['prefix_fingerprint']!=r['prefix_fingerprint'] else []
                    anomalies.append({'at':r['at'],'request_id':r['request_id'],'from_rate':a,'to_rate':b,'possible_causes':reasons+changed or ['provider_cache_expiry_or_routing_unknown'],'inference':True})
            prior[key]={k:r.get(k) for k in ('model','cache_policy','price','config_revision','prefix_fingerprint')};prior[key].update(context_limit=limit,rate=r['normalized']['cache_hit_rate'])
            if trends_complete:
                for period,at in [('hour',local.replace(minute=0,second=0,microsecond=0).isoformat()),('day',local.date().isoformat())]:
                    key=(period,at,r['provider_id'],r['model'])
                    if key not in trends and len(trends)>=2000:trends_complete=False;trends.clear();break
                    trends.setdefault(key,Summary()).add(r)
        for e in ledger.iter_events(mode,None if all_sessions else sid,conn=conn):
            if e['kind'] in {'compaction','continuation','model_switch'}:history.append(e)
            if not all_sessions:
                if e['kind']=='message_counts':counts=e.get('snapshot')
                if e['kind']=='context':observation=e
                if e['kind']=='compaction' and (e.get('snapshot') or {}).get('dispatched') is not False:invalidated=e['at']
        round_summary=Summary()
        if last:
            for r in ledger.iter_records(mode,last['session_id'],model=model,turn=last['turn_id'],conn=conn):round_summary.add(r)
        health=coverage(ledger,mode,None if all_sessions else sid,conn=conn)
        budget_health=coverage(ledger,mode,conn=conn)
        health['budget_has_observed_gaps']=budget_health['has_observed_gaps']
    obs=observation.get('snapshot') if observation else None;observed_at=(obs or {}).get('observed_at') or (observation or {}).get('at')
    if latest_scope and not all_sessions and isinstance(latest_scope.get('context'),dict):
        at=latest_scope['context'].get('observed_at') or latest_scope['at']
        if observed_at is None or timestamp(at)>timestamp(observed_at):obs=latest_scope['context'];observed_at=at
    if obs and latest_scope and observed_at and timestamp(latest_scope['at'])>timestamp(observed_at):obs={**obs,'stale':True}
    if obs and observed_at and invalidated and timestamp(invalidated)>=timestamp(observed_at):obs={**obs,'tokens':None,'stale':True}
    counts={k:count((counts or {}).get(k)) for k in ('total','visible','folded','archived','turns')};provenance={'source':'recorded_snapshot','status':'unverified'}
    if message_source is not None:
        result=message_source(mode,sid,all_sessions=all_sessions);counts={k:count(result['counts'].get(k)) for k in counts};provenance=result['provenance']
    summary=total.value();day=daily.value();month=monthly.value();budget_views=[]
    for b in budgets or []:
        if b.get('period') not in {'day','month'} or number(b.get('limit')) is None or not re.fullmatch('[A-Z]{3}',str(b.get('currency',''))):raise LedgerError('ledger_budget_invalid')
        c=next((c for c in (day if b['period']=='day' else month)['costs'] if c['currency']==b['currency']),None)
        known=Decimal(c['amount']) if c and c['amount'] is not None else None
        complete=bool(c and c['complete']) and not health['retention']['records_removed'] and not budget_health['has_observed_gaps']
        budget_views.append({**b,'known_spend':str(known) if known is not None else None,'remaining':str(max(Decimal(0),number(b['limit'])-known)) if known is not None and complete else None,'complete':complete,'exceeded':known>=number(b['limit']) if known is not None else None,'enforcement':'warning_only'})
    observed_context=context(obs,policy if settings['revision'] else (latest_scope or {}).get('policy'))
    return {'ok':True,'contract_version':1,'mode':mode,'session_id':sid,'scope':'all' if all_sessions else 'session','timezone':zone,
        'coverage':'recorded_turns_only' if mode=='codex' else 'recorded_requests_only','collection_health':health,
        'context':observed_context,
        'rollover_preview':rollover_preview(observed_context,observed_at=observed_at,all_sessions=all_sessions,unconfirmed=health['unconfirmed']),
        'summary':summary,'daily':day,'monthly':month,'last_round':round_summary.value(),
        'message_counts':counts,'message_counts_provenance':provenance,'events':list(history),'configuration_changes':list(changes),'cache_diagnostics':list(anomalies),
        'trends':[{'period':k[0],'at':k[1],'provider_id':k[2],'model':k[3],'summary':v.value()} for k,v in sorted(trends.items())],'trends_complete':trends_complete,
        'budgets':budget_views,'settings':settings,'models':sorted(models),'recent_requests':list(recent)}
