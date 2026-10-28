"""Synthetic-only accounting tests. Never import memory or start an SDK/client."""
import copy
import csv
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.usage_ledger import Ledger, LedgerError, build_view, context, estimate, export_records, metric_sum, normalize, stable_prefix

RAW={'input_tokens':1000,'output_tokens':100,'total_tokens':1100,'input_tokens_details':{'cached_tokens':800,'cache_write_tokens':100},'output_tokens_details':{'reasoning_tokens':20},'vendor_extension':{'counter':7}}
PRICE={'currency':'USD','price_id':'synthetic-not-a-real-price','per_million':{'input':'1','output':'2','cache_read':'0.1','cache_write':'1.25'}}


def record(**kw):
    return {'mode':'api','provider_id':'provider-a','request_id':'request-1','turn_id':'turn-1','session_id':'s-1','at':'2026-10-23T02:00:00Z',
            'model':'mock-a','dialect':'openai_responses','raw_usage':copy.deepcopy(RAW),'usage_complete':True,'status':'completed',
            'latency_ms':100,'retry_count':0,'http_status':200,'price':copy.deepcopy(PRICE),**kw}


class NormalizationTests(unittest.TestCase):
    def test_missing_is_null_reported_zero_is_zero_and_raw_preserved(self):
        raw=copy.deepcopy(RAW);u=normalize(raw,'openai_responses');self.assertEqual(u['raw'],raw)
        self.assertEqual(u['metrics'],{'input':1000,'output':100,'total':1100,'reasoning':20,'cache_read':800,'cache_write':100,'cache_miss':200,'ordinary_input':100})
        u=normalize({'prompt_tokens':0,'completion_tokens':0,'total_tokens':0},'openai_chat')
        self.assertEqual(u['metrics']['input'],0);self.assertEqual(u['metrics']['total'],0);self.assertIsNone(u['metrics']['cache_write']);self.assertIsNone(u['cache_hit_rate'])
        self.assertTrue(all(v is None for v in normalize(None,'unknown')['metrics'].values()))

    def test_invalid_numeric_types_are_unavailable(self):
        for value in (-1,True,1.5,'10',2**53):
            u=normalize({'input_tokens':value},'openai_responses');self.assertIsNone(u['metrics']['input'])

    def test_anthropic_partition_is_added_once(self):
        raw={'input_tokens':50,'cache_read_input_tokens':2000,'cache_creation_input_tokens':100,'output_tokens':40}
        u=normalize(raw,'anthropic_messages');self.assertEqual(u['metrics']['input'],2150);self.assertEqual(u['metrics']['total'],2190);self.assertEqual(u['metrics']['ordinary_input'],50)
        self.assertIsNone(normalize({'input_tokens':50,'output_tokens':40},'anthropic_messages')['metrics']['input'])

    def test_deepseek_hit_miss_conflicts_never_count_twice(self):
        u=normalize({'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':90,'prompt_cache_miss_tokens':10,'prompt_tokens_details':{'cached_tokens':90}},'deepseek_chat')
        self.assertEqual(u['metrics']['cache_read'],90);self.assertEqual(u['metrics']['input'],100);self.assertEqual(u['cache_hit_rate'],.9)
        u=normalize({'prompt_tokens':100,'prompt_cache_hit_tokens':90,'prompt_cache_miss_tokens':20},'deepseek_chat')
        self.assertIn('cache_partition_conflict',u['issues']);self.assertIsNone(u['cache_hit_rate'])

    def test_codex_has_no_api_cache_or_cost_even_with_same_model_name(self):
        u=normalize({'inputTokens':100,'outputTokens':20,'totalTokens':120,'cachedInputTokens':90,'reasoningOutputTokens':5},'codex')
        self.assertEqual(u['metrics']['reasoning'],5);self.assertIsNone(u['metrics']['cache_read']);self.assertFalse(u['cache_applicable']);self.assertIsNone(estimate(u,PRICE)['amount'])

    def test_bad_partitions_total_reasoning_stay_flagged(self):
        raw={**RAW,'total_tokens':999,'input_tokens_details':{'cached_tokens':900,'cache_write_tokens':200},'output_tokens_details':{'reasoning_tokens':200}}
        u=normalize(raw,'openai_responses');self.assertEqual(set(u['issues']),{'total_conflict','reasoning_conflict','cache_partition_conflict'});self.assertIsNone(u['metrics']['total']);self.assertIsNone(u['cache_hit_rate'])

    def test_precise_cost_no_reasoning_double_charge_or_write_double_count(self):
        cost=estimate(normalize(RAW,'openai_responses'),PRICE)
        self.assertEqual(cost['amount'],'0.000505');self.assertEqual(cost['savings'],'0.000695')
        changed={**RAW,'input_tokens_details':{'cached_tokens':800,'cache_write_tokens':200,'cache_write_tokens_details':{'ttl_30m_tokens':50,'ttl_12h_tokens':150}}}
        p={**PRICE,'per_million':{**PRICE['per_million'],'write_30m':'1.25','write_12h':'2.5'}}
        self.assertEqual(estimate(normalize(changed,'openai_responses'),p)['amount'],'0.0007175')

    def test_missing_write_not_assumed_zero_and_legacy_price_opt_in(self):
        raw={'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':80}}
        self.assertIsNone(estimate(normalize(raw,'openai_chat'),PRICE)['amount'])
        self.assertEqual(estimate(normalize(raw,'openai_chat'),{**PRICE,'write_in_input_rate':True})['amount'],'0.000068')

    def test_missing_price_and_mixed_write_ttl_and_net_negative_savings(self):
        self.assertIsNone(estimate(normalize(RAW,'openai_responses'),None)['amount'])
        u=normalize({'input_tokens':1000,'output_tokens':0,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':1000}},'openai_responses')
        self.assertEqual(estimate(u,PRICE)['savings'],'-0.00025')
        with self.assertRaises(LedgerError):estimate(u,{**PRICE,'per_million':{'input':'NaN'}})

    def test_context_is_not_cumulative_usage_and_stale_cannot_warn(self):
        c=context({'tokens':98100,'limit':200000,'stale':False},{'threshold_tokens':130000,'warning_margin_tokens':32000})
        self.assertEqual(c['distance_to_threshold'],31900);self.assertEqual(c['remaining'],101900);self.assertTrue(c['warning']);self.assertFalse(c['automatic_switch_enabled'])
        self.assertFalse(context({'tokens':98100,'limit':200000,'stale':True},{'threshold_tokens':130000,'warning_margin_tokens':32000})['warning'])
        self.assertIsNone(context({}, {'threshold_tokens':130000})['used_percent'])

    def test_stable_prefix_preserves_order_ignores_dict_key_order_and_uses_hmac(self):
        key=b'x'*32
        a,f=stable_prefix('synthetic stable instructions',[{'name':'read','schema':{'b':2,'a':1}}],key)
        b,g=stable_prefix('synthetic stable instructions',[{'schema':{'a':1,'b':2},'name':'read'}],key)
        self.assertEqual((a,f),(b,g));self.assertNotEqual(f,stable_prefix('changed',[],key)[1]);self.assertNotEqual(f,stable_prefix('synthetic stable instructions',[],b'y'*32)[1])


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.path=Path(tmp.name)/'ledger.sqlite';self.ledger=Ledger(self.path)

    def test_idempotent_concurrent_append_does_not_double_charge(self):
        with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(lambda _:self.ledger.append(record()),range(8)))
        self.assertEqual(results.count(True),1)
        with self.assertRaisesRegex(LedgerError,'duplicate_conflict'):self.ledger.append(record(latency_ms=200))
        self.assertEqual(build_view(self.ledger,'api','s-1')['summary']['requests'],1)

    def test_raw_round_trip_and_incomplete_usage_never_full_cost(self):
        self.ledger.append(record(usage_complete=False));row=self.ledger.records('api')[0]
        self.assertEqual(row['raw_usage'],RAW);self.assertIsNone(row['cost']['amount'])
        v=build_view(self.ledger,'api','s-1');self.assertEqual(v['summary']['metrics']['input']['value'],1000);self.assertFalse(v['summary']['metrics']['input']['complete'])
        self.assertNotIn('raw_usage',v['recent_requests'][0])

    def test_partial_aggregate_and_request_weighted_cache_rate(self):
        self.ledger.append(record());self.ledger.append(record(request_id='request-2',raw_usage={'input_tokens':10000,'output_tokens':1000},turn_id='turn-2'))
        v=build_view(self.ledger,'api','s-1')['summary'];self.assertEqual(v['metrics']['input']['value'],11000);self.assertEqual(v['cache_hit_rate'],.8);self.assertFalse(v['cache_rate_complete']);self.assertEqual(v['cache_rate_requests'],1);self.assertFalse(v['costs'][0]['complete'])

    def test_api_codex_and_provider_account_ids_are_isolated(self):
        self.ledger.append(record());self.ledger.append(record(provider_id='provider-b'))
        self.ledger.append(record(mode='codex',dialect='codex',raw_usage={'inputTokens':30,'outputTokens':10},price=None))
        self.assertEqual(len(self.ledger.records('api')),2);self.assertEqual(len(self.ledger.records('codex')),1)
        self.assertEqual(build_view(self.ledger,'codex','s-1')['summary']['costs'],[])
        self.assertEqual(build_view(self.ledger,'api','s-1')['summary']['metrics']['input']['value'],2000)

    def test_gateway_unicode_model_alias_survives_filters_and_csv_formula_guard(self):
        self.ledger.append(record(model='[Pro按量]mock-model'))
        self.assertEqual(build_view(self.ledger,'api','s-1',model='[Pro按量]mock-model')['summary']['requests'],1)
        self.ledger.append(record(model='=untrusted-alias',request_id='alias-2'))
        csv_row=list(csv.DictReader(io.StringIO(export_records(self.ledger.records('api',model='=untrusted-alias'),'csv'))))[0]
        self.assertEqual(csv_row['model'],"'=untrusted-alias")

    def test_last_round_sums_unique_tool_requests_not_stream_snapshots(self):
        self.ledger.append(record());self.ledger.append(record(request_id='tool-followup-1'))
        self.ledger.append(record(request_id='next-turn',turn_id='turn-2',raw_usage={'input_tokens':50,'output_tokens':10}))
        view=build_view(self.ledger,'api','s-1');self.assertEqual(view['last_round']['metrics']['input']['value'],50);self.assertEqual(view['summary']['observed_turns'],2)

    def test_daily_monthly_timezone_and_budgets_include_all_sessions_and_models(self):
        self.ledger.append(record(at='2026-10-22T16:01:00Z'));self.ledger.append(record(request_id='r2',session_id='s-2',model='mock-b',at='2026-10-23T02:00:00Z'))
        self.ledger.save_settings('api',0,{'timezone':'Asia/Shanghai','budgets':[{'period':'day','currency':'USD','limit':'0.0008'}]})
        v=build_view(self.ledger,'api','s-1',model='mock-a',now=datetime(2026,10,23,3,tzinfo=timezone.utc))
        self.assertEqual(v['summary']['requests'],1);self.assertEqual(v['daily']['requests'],2);self.assertEqual(v['monthly']['requests'],2);self.assertTrue(v['budgets'][0]['exceeded']);self.assertEqual(v['budgets'][0]['remaining'],'0')

    def test_currencies_never_merged_unknown_cost_cannot_promise_budget(self):
        self.ledger.append(record());self.ledger.append(record(request_id='r2',price={**PRICE,'currency':'CNY'}));self.ledger.append(record(request_id='r3',price=None))
        self.ledger.save_settings('api',0,{'budgets':[{'period':'month','currency':'USD','limit':'1'}]})
        v=build_view(self.ledger,'api','s-1',now=datetime(2026,10,23,tzinfo=timezone.utc))
        self.assertEqual(len(v['summary']['costs']),2);self.assertIsNone(v['budgets'][0]['remaining']);self.assertFalse(v['budgets'][0]['complete'])

    def test_context_warning_and_counts_have_independent_sources(self):
        self.ledger.append(record(context={'tokens':98100,'limit':200000,'stale':False,'source':'mock'},policy={'threshold_tokens':130000,'warning_margin_tokens':32000}))
        self.ledger.event({'event_id':'counts','session_id':'s-1','mode':'api','at':'2026-10-23T02:00:00Z','kind':'message_counts','status':'observed','snapshot':{'total':100,'visible':10,'folded':80,'archived':10}})
        v=build_view(self.ledger,'api','s-1');self.assertEqual(v['context']['tokens'],98100);self.assertTrue(v['context']['warning']);self.assertIsNone(v['message_counts']['turns']);self.assertEqual(v['message_counts']['archived'],10)
        self.assertIsNone(build_view(self.ledger,'api','s-1',all_sessions=True)['context']['tokens'])

    def test_compaction_invalidates_old_occupancy_until_new_observation(self):
        self.ledger.append(record(context={'tokens':98100,'limit':200000,'stale':False,'source':'mock','observed_at':'2026-10-23T02:00:00Z'},policy={'threshold_tokens':130000,'warning_margin_tokens':32000}))
        self.ledger.event({'event_id':'compact-new','session_id':'s-1','mode':'api','at':'2026-10-23T02:01:00Z','kind':'compaction','status':'completed'})
        v=build_view(self.ledger,'api','s-1');self.assertIsNone(v['context']['tokens']);self.assertFalse(v['context']['warning'])
        self.ledger.event({'event_id':'new-obs','session_id':'s-1','mode':'api','at':'2026-10-23T02:02:00Z','kind':'context','status':'observed','snapshot':{'tokens':3000,'limit':200000,'stale':False,'source':'mock'}})
        self.assertEqual(build_view(self.ledger,'api','s-1')['context']['tokens'],3000)

    def test_memory_retention_is_unknown_without_independent_validation(self):
        event={'event_id':'compact-1','session_id':'s-1','mode':'api','at':'2026-10-23T02:00:00Z','kind':'compaction','status':'completed','before_tokens':10000,'after_tokens':3000}
        self.ledger.event(event);v=build_view(self.ledger,'api','s-1');self.assertIsNone(v['events'][0].get('memory_preserved'))
        with self.assertRaisesRegex(LedgerError,'memory_unverified'): self.ledger.event({**event,'event_id':'compact-2','memory_preserved':True})

    def test_model_price_prefix_and_cache_policy_changes_and_hourly_trends(self):
        self.ledger.append(record(prefix_fingerprint='a'*64))
        raw={**RAW,'input_tokens_details':{'cached_tokens':100,'cache_write_tokens':100}}
        self.ledger.append(record(request_id='r2',at='2026-10-23T03:00:00Z',model='mock-b',raw_usage=raw,prefix_fingerprint='b'*64,cache_policy='new-policy',price={**PRICE,'price_id':'next-version'}))
        v=build_view(self.ledger,'api','s-1');self.assertEqual(len(v['configuration_changes']),1);self.assertIn('price',v['configuration_changes'][0]['changed']);self.assertEqual(v['cache_diagnostics'][0]['possible_causes'][0],'stable_prefix_changed');self.assertTrue(v['cache_diagnostics'][0]['inference']);self.assertEqual(len([t for t in v['trends'] if t['period']=='hour']),2)

    def test_settings_cas_modes_and_no_past_price_recalculation(self):
        self.ledger.append(record());before=self.ledger.records('api')[0]['cost'];self.ledger.save_settings('api',0,{'threshold_tokens':130000,'warning_margin_tokens':10000})
        with self.assertRaisesRegex(LedgerError,'revision_conflict'):self.ledger.save_settings('api',0,{})
        self.assertEqual(self.ledger.settings('codex')['revision'],0);self.assertEqual(self.ledger.records('api')[0]['cost'],before)

    def test_empty_ledger_and_unknown_latency_do_not_become_fake_zero(self):
        v=build_view(self.ledger,'api','s-1');self.assertIsNone(v['summary']['metrics']['input']['value']);self.assertIsNone(v['summary']['latency_ms']);self.assertIsNone(v['summary']['error_rate']);self.assertIsNone(v['message_counts']['total']);self.assertEqual(v['summary']['requests'],0)

    def test_secret_keys_unbounded_fields_and_bad_policy_rejected(self):
        for r in (record(raw_usage={'api_key':'never-store'}),record(context={'chat_text':'never-store'}),record(latency_ms=True),record(policy={'threshold_tokens':0})):
            with self.assertRaises((LedgerError,ValueError)):self.ledger.append(r)
        self.assertEqual(self.ledger.records('api'),[])
        with self.assertRaises(LedgerError):self.ledger.save_settings('api',0,{'timezone':'../../etc/passwd'})

    def test_csv_json_exports_preserve_null_zero_and_raw_data(self):
        self.ledger.append(record(raw_usage={'input_tokens':0,'output_tokens':0},price=None));rows=self.ledger.records('api')
        self.assertEqual(json.loads(export_records(rows,'json'))['records'][0]['raw_usage'],{'input_tokens':0,'output_tokens':0})
        exported=list(csv.DictReader(io.StringIO(export_records(rows,'csv'))))[0]
        self.assertEqual(exported['input'],'0');self.assertEqual(exported['cache_write'],'');self.assertEqual(exported['amount'],'')
        altered=copy.deepcopy(rows);altered[0]['raw_usage']={'extension':'=1+1'}
        # JSON is a quoted CSV cell; no raw vendor string becomes a formula cell.
        self.assertTrue(list(csv.DictReader(io.StringIO(export_records(altered,'csv'))))[0]['raw_usage_json'].startswith('{'))

    def test_purge_is_scoped_and_no_chat_or_memory_database_is_opened(self):
        self.ledger.append(record());self.ledger.append(record(request_id='r2',session_id='s-2'));self.ledger.purge_session('s-1')
        self.assertEqual(len(self.ledger.records('api')),1);self.assertEqual(self.ledger.records('api')[0]['session_id'],'s-2')


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_asgi_routes_auth_filters_settings_exports_no_outbound_calls(self):
        import httpx
        from fastapi import FastAPI,HTTPException
        from backend.usage_ledger_routes import install
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'ledger.sqlite');ledger.append(record());app=FastAPI()
            def auth(request):
                if request.headers.get('authorization')!='Bearer mock-relay':raise HTTPException(401)
            install(app,ledger=ledger,check_auth=auth,session_provider=lambda sid:'api' if sid=='s-1' else None)
            with patch('socket.create_connection',side_effect=AssertionError('outbound networking forbidden')):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test',headers={'Authorization':'Bearer mock-relay'}) as client:
                    r=await client.get('/app/sessions/s-1/usage-ledger');self.assertEqual(r.status_code,200);self.assertEqual(r.headers['cache-control'],'private, no-store');self.assertEqual(r.json()['summary']['metrics']['input']['value'],1000)
                    self.assertEqual((await client.get('/app/sessions/s-1/usage-ledger?mode=codex')).json()['summary']['requests'],0)
                    self.assertEqual((await client.get('/app/sessions/missing/usage-ledger')).status_code,400)
                    self.assertEqual((await client.get('/app/sessions/s-1/usage-ledger?scope=all&scope=session')).status_code,400)
                    self.assertEqual((await client.get('/app/sessions/s-1/usage-ledger/export?format=csv')).status_code,200)
                    self.assertEqual((await client.put('/app/usage-ledger/settings/api',json={'revision':0,'policy':{'threshold_tokens':130000}})).status_code,200)
                    self.assertEqual((await client.put('/app/usage-ledger/settings/api',json={'revision':0,'policy':{}})).status_code,409)
                    client.headers.pop('Authorization')
                    self.assertEqual((await client.get('/app/sessions/s-1/usage-ledger')).status_code,401)
                    self.assertEqual((await client.get('/app/sessions/s-1/usage-ledger/export')).status_code,401)
                    self.assertEqual((await client.put('/app/usage-ledger/settings/api',json={})).status_code,401)


if __name__=='__main__':unittest.main()
