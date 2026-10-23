"""Offline production-path tests: HTTPX MockTransport, synthetic data only.

Load only the provider functions from the real bridge source so testing cannot
import memory modules, load .env, start SDKs or initialize production workers.
"""
import ast
import __future__
import asyncio
import copy
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend import api_mcp_model as mcp, usage_ledger_capture as capture
from backend.usage_ledger import Ledger, LedgerError, build_view
from backend.usage_ledger_capture import ApiCollector, CachePolicy, RouteProfile, capture_scope

ROOT = Path(__file__).resolve().parents[2]
ROUTE = {'url': 'https://synthetic.invalid/v1', 'model': 'synthetic-model', 'key': 'synthetic-not-real-key'}
PRICE = {'currency': 'USD', 'price_id': 'synthetic-only', 'write_in_input_rate': True,
         'per_million': {'input': '1', 'output': '2', 'cache_read': '0.1'}}
RAW = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
       'prompt_tokens_details': {'cached_tokens': 80}, 'completion_tokens_details': {'reasoning_tokens': 5}}


def provider_functions(handler):
    names = {'ModelRouteError', '_safe_provider_http_status', '_safe_provider_error_identifier',
             '_provider_error_metadata', '_read_provider_error_metadata', '_check_provider_response',
             '_responses_event_error_metadata', '_chat_completion_body', '_responses_body', '_responses_text',
             'stream_responses', 'stream_chat', 'complete_responses', 'complete_chat', '_provider_client',
             '_uses_responses_api', '_stream_provider', '_complete_provider', 'run_model'}
    source = ast.parse((ROOT/'examples/api_loop.py').read_text())
    nodes = [n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    if len(nodes) != len(names): raise AssertionError('provider test source contract changed')
    ns = {'asyncio': asyncio, 'json': json, 'Any': object, 'usage_ledger_capture': capture,
          'httpx': SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)),
          'deployment_config': SimpleNamespace(parse_strict_bool=lambda value, _: value == '1'),
          'os': SimpleNamespace(environ={}), 'MAX_TOKENS': 100, 'TEMPERATURE': 0.7, 'STREAM_OUTPUT': True,
          'LOOP_MODEL_TOTAL_TIMEOUT_SECONDS': 2, 'LOOP_PROVIDER_RESPONSE_MAX_BYTES': 2*1024*1024,
          'LOOP_ASSISTANT_MAX_CHARS': 64000, 'RESPONSES_API_MODELS': set(),
          'SAFE_FALLBACK_ERROR_CODES': {'model_not_found'}, 'PROVIDER_ERROR_IDENTIFIER_RE': __import__('re').compile(r'^[a-zA-Z0-9_.:-]+$'),
          '_safe_log': lambda *args: None, 'main_chain': lambda: [copy.deepcopy(ROUTE)]}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT/'examples/api_loop.py'), 'exec', flags=__future__.annotations.compiler_flag), ns)
    return ns


def sse(events, done=True):
    body = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)
    return httpx.Response(200, text=body+('data: [DONE]\n\n' if done else ''), headers={'Content-Type': 'text/event-stream'})


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(Path(tmp.name)/'usage.sqlite')
        self.collector = self.make_collector()
        # Even an accidentally unmocked provider call must never leave this test.
        for name in ('create_connection', 'getaddrinfo'):
            self.enterContext(patch.object(socket, name, side_effect=AssertionError('offline-only')))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))

    def make_collector(self, *, protocol='chat', dialect='openai_chat', cache=CachePolicy(), profiles=None, ledger=None):
        return ApiCollector(ledger or self.ledger, profiles or [RouteProfile(ROUTE['url'], ROUTE['model'], protocol,
            'synthetic-account', dialect, 'version-1', cache, PRICE)], fingerprint_key=b'synthetic-key-for-tests-only-12345')

    async def test_disabled_and_unmatched_profiles_preserve_exact_body_and_no_ledger(self):
        bodies = []
        async def handler(req):
            bodies.append(json.loads(req.content)); return httpx.Response(200, json={'choices':[{'message':{'content':'synthetic'}}], 'usage':RAW})
        ns = provider_functions(handler); messages = [{'role':'system','content':'stable'}, {'role':'user','content':'synthetic'}]
        expected = ns['_chat_completion_body'](ROUTE, messages, stream=False)
        await ns['complete_chat'](ROUTE, messages)
        self.assertEqual(self.ledger.records('api'), []); self.assertEqual(bodies[0], expected)
        with capture_scope(self.collector, 'session-a', 'turn-a'):
            await ns['complete_chat']({**ROUTE, 'model':'unconfigured-model'}, messages)
        self.assertEqual(self.ledger.records('api'), []); self.assertEqual(self.collector.health()['unmatched'], 1)
        self.assertFalse(self.collector.health()['complete'])

    async def test_chat_stream_snapshots_replace_not_sum_and_missing_usage_stays_unavailable(self):
        async def handler(req):
            return sse([{'choices':[{'delta':{'content':'hello'}}], 'usage':RAW},
                        {'choices':[], 'usage':RAW}, {'choices':[], 'usage':{**RAW, 'vendor':{'counter':9}}}])
        ns = provider_functions(handler); chunks = []
        with capture_scope(self.collector, 'session-a', 'turn-a'):
            await ns['stream_chat'](ROUTE, [{'role':'user','content':'synthetic'}], AsyncMock(side_effect=chunks.append))
        rows = self.ledger.records('api'); self.assertEqual(len(rows), 1); row = rows[0]
        self.assertEqual(row['normalized']['metrics']['input'], 100); self.assertEqual(row['raw_usage']['vendor'], {'counter':9})
        self.assertEqual(row['status'], 'completed'); self.assertEqual(row['http_status'], 200); self.assertTrue(row['usage_complete'])
        self.assertEqual(row['model_source'], 'request'); self.assertNotIn(ROUTE['key'], json.dumps(rows))
        ns = provider_functions(lambda req:sse([{'choices':[{'delta':{'content':'no usage'}}]}]))
        with capture_scope(self.collector, 'session-a', 'turn-b'):
            await ns['stream_chat'](ROUTE, [], AsyncMock())
        row = self.ledger.records('api')[-1]; self.assertIsNone(row['raw_usage']); self.assertFalse(row['usage_complete'])
        self.assertIsNone(row['cost']['amount']); self.assertIsNone(row['normalized']['metrics']['input'])

    async def test_responses_stream_and_nonstream_record_original_usage(self):
        raw = {'input_tokens':300, 'output_tokens':40, 'total_tokens':340, 'input_tokens_details':{'cached_tokens':250,'cache_write_tokens':50}}
        collector = self.make_collector(protocol='responses', dialect='openai_responses')
        for stream in (False, True):
            async def handler(req):
                data = {'status':'completed','model':ROUTE['model'],'output_text':'synthetic','usage':raw}
                return sse([{'type':'response.completed','response':data}], done=False) if stream else httpx.Response(200,json=data)
            ns = provider_functions(handler)
            with capture_scope(collector, 'session-a', 'stream' if stream else 'complete'):
                if stream: await ns['stream_responses'](ROUTE, [], AsyncMock())
                else: await ns['complete_responses'](ROUTE, [])
        rows = self.ledger.records('api'); self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['raw_usage'] == raw and r['usage_complete'] for r in rows))
        self.assertTrue(all(r['model_source'] == 'provider' for r in rows))

    async def test_fallback_records_each_attempt_without_turn_aggregate_double_count(self):
        async def handler(req):
            if json.loads(req.content)['model'] == ROUTE['model']:
                return httpx.Response(404,json={'error':{'code':'model_not_found'}})
            return httpx.Response(200,json={'choices':[{'message':{'content':'fallback'}}], 'usage':RAW})
        ns = provider_functions(handler); fallback = {**ROUTE,'model':'fallback-model'}
        ns['main_chain'] = lambda:[ROUTE,fallback]
        profiles = [RouteProfile(ROUTE['url'], r['model'], 'chat', 'synthetic-account', 'openai_chat', 'v1', price=PRICE) for r in (ROUTE,fallback)]
        collector = self.make_collector(profiles=profiles)
        with capture_scope(collector, 'session-a', 'turn-a'):
            result = await ns['run_model']([], emit_stream=False)
        self.assertEqual(result['outcome'], 'success')
        rows = self.ledger.records('api'); self.assertEqual(len(rows), 2)
        self.assertEqual([r['status'] for r in rows], ['error','completed']); self.assertEqual(rows[0]['http_status'],404)
        view = build_view(self.ledger,'api','session-a'); self.assertEqual(view['summary']['metrics']['input']['value'],100)
        self.assertFalse(view['last_round']['metrics']['input']['complete'])

    async def test_http_errors_timeouts_cancellation_and_partial_usage_are_truthful(self):
        for status, expected in ((429,'rate_limited'), (408,'timeout'), (503,'error')):
            ns = provider_functions(lambda req:httpx.Response(status,json={'error':{'message':'synthetic secret body'}}))
            with capture_scope(self.collector, 'session-a', 'http-'+str(status)):
                with self.assertRaises(ns['ModelRouteError']): await ns['complete_chat'](ROUTE, [])
            self.assertEqual(self.ledger.records('api')[-1]['status'], expected)
        async def timeout(req): raise httpx.ReadTimeout('synthetic private URL')
        ns = provider_functions(timeout)
        with capture_scope(self.collector, 'session-a', 'transport-timeout'):
            with self.assertRaises(ns['ModelRouteError']): await ns['stream_chat'](ROUTE, [], AsyncMock())
        self.assertEqual(self.ledger.records('api')[-1]['status'],'timeout')
        ns = provider_functions(lambda req:sse([{'choices':[{'delta':{'content':'partial'}}], 'usage':RAW}]))
        async def cancel(chunk): raise asyncio.CancelledError()
        with capture_scope(self.collector, 'session-a', 'cancel'):
            with self.assertRaises(asyncio.CancelledError): await ns['stream_chat'](ROUTE, [], cancel)
        row = self.ledger.records('api')[-1]; self.assertEqual(row['status'],'interrupted'); self.assertEqual(row['raw_usage'],RAW)
        self.assertFalse(row['usage_complete']); self.assertIsNone(row['cost']['amount'])
        self.assertNotIn('private URL',json.dumps(self.ledger.records('api')))

    async def test_outer_model_deadline_is_timeout_not_user_interruption(self):
        async def handler(req): await asyncio.Event().wait()
        ns = provider_functions(handler); ns['LOOP_MODEL_TOTAL_TIMEOUT_SECONDS'] = .01
        with capture_scope(self.collector, 'session-a', 'deadline'):
            result = await ns['run_model']([], emit_stream=False)
        self.assertEqual(result['error'], 'model_timeout'); self.assertEqual(self.ledger.records('api')[0]['status'], 'timeout')

    async def test_capture_failure_does_not_change_result_retry_or_leak_errors(self):
        broken = SimpleNamespace(append=lambda _: (_ for _ in ()).throw(OSError('synthetic private path')))
        collector = self.make_collector(ledger=broken); calls = []
        async def handler(req):
            calls.append(req); return httpx.Response(200,json={'choices':[{'message':{'content':'same result'}}], 'usage':RAW})
        ns = provider_functions(handler)
        with capture_scope(collector, 'session-a', 'turn-a'):
            out = await ns['complete_chat'](ROUTE, [])
        self.assertEqual(out['text'],'same result'); self.assertEqual(len(calls),1)
        self.assertEqual(collector.health()['dropped'],1); self.assertFalse(collector.health()['complete'])
        self.assertNotIn('private path',json.dumps(collector.health()))

    async def test_explicit_cache_profile_stable_prefix_and_tool_order_preserve_prompt(self):
        bodies = []
        async def handler(req):
            bodies.append(json.loads(req.content)); return httpx.Response(200,json={'status':'completed','output_text':'synthetic','usage':{'input_tokens':10,'output_tokens':2}})
        collector = self.make_collector(protocol='responses', dialect='openai_responses', cache=CachePolicy('openai_responses_implicit','30m'))
        ns = provider_functions(handler)
        original = [{'role':'system','content':'synthetic stable memory order'}, {'role':'user','content':'first'}]
        for text in ('first','second'):
            messages = copy.deepcopy(original); messages[-1]['content'] = text
            with capture_scope(collector, 'session-a', 'turn-'+text): await ns['complete_responses'](ROUTE, messages)
            self.assertEqual(bodies[-1]['input'],messages)
        self.assertEqual(bodies[0]['prompt_cache_key'],bodies[1]['prompt_cache_key'])
        self.assertEqual(bodies[0]['prompt_cache_options'],{'mode':'implicit','ttl':'30m'})
        self.assertNotIn('prewarm',json.dumps(bodies)); self.assertNotIn(original[0]['content'],json.dumps(self.ledger.records('api')))
        self.assertEqual(self.ledger.records('api')[0]['cache_policy'],'openai_responses_implicit-30m')
        # Semantic tool list order is neither sorted nor mutated for cache reuse.
        with capture_scope(collector, 'session-b', 'tools'):
            @capture.observe_api(protocol='responses')
            async def prepare(route):
                first = {'input':original, 'tools':[{'name':'b'},{'name':'a'}]}
                changed = {**first,'tools':list(reversed(first['tools']))}
                a = capture.prepare_body(first,responses=True); b = capture.prepare_body(changed,responses=True)
                self.assertEqual(a['tools'],first['tools']); self.assertNotEqual(a['prompt_cache_key'],b['prompt_cache_key'])
            await prepare(ROUTE)

    async def test_automatic_cache_adds_no_provider_fields_and_rerouted_model_has_no_guessed_price(self):
        bodies = []
        async def handler(req):
            bodies.append(json.loads(req.content)); return httpx.Response(200,json={'model':'actual-other-model','choices':[{'message':{'content':'synthetic'}}], 'usage':{'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':80,'prompt_cache_miss_tokens':20}})
        collector = self.make_collector(dialect='deepseek_chat',cache=CachePolicy('automatic'))
        ns = provider_functions(handler)
        with capture_scope(collector, 'session-a', 'turn-a'): await ns['complete_chat'](ROUTE, [])
        self.assertFalse(any(k.startswith('prompt_cache') for k in bodies[0]))
        row = self.ledger.records('api')[0]; self.assertEqual(row['normalized']['metrics']['cache_read'],80)
        self.assertEqual(row['requested_model'],ROUTE['model']); self.assertEqual(row['model'],'actual-other-model')
        self.assertEqual(row['model_source'],'provider'); self.assertIsNone(row['price']); self.assertIsNone(row['cost']['amount'])

    async def test_parallel_session_scopes_and_unsafe_usage_are_isolated(self):
        async def handler(req):
            await asyncio.sleep(0)
            value = json.loads(req.content)['messages'][0]['content']
            return httpx.Response(200,json={'choices':[{'message':{'content':'synthetic'}}], 'usage':{**RAW, **({'authorization':'synthetic-secret'} if value == 'bad' else {})}})
        ns = provider_functions(handler)
        async def run(sid):
            with capture_scope(self.collector, sid, 'turn-'+sid): await ns['complete_chat'](ROUTE, [{'role':'user','content':sid}])
        await asyncio.gather(run('good'),run('bad'))
        good = self.ledger.records('api','good')[0]; bad = self.ledger.records('api','bad')[0]
        self.assertEqual(good['raw_usage'],RAW); self.assertIsNone(bad['raw_usage']); self.assertFalse(bad['usage_complete'])
        self.assertNotIn('synthetic-secret',json.dumps([good,bad])); self.assertEqual(self.collector.health()['captured'],2)

    async def test_mcp_requests_use_same_capture_without_summing_aggregate_twice(self):
        for responses in (False,True):
            with self.subTest(responses=responses):
                calls = []
                async def handler(req):
                    body=json.loads(req.content); calls.append(body)
                    if responses:
                        output=[{'type':'function_call','call_id':'call-1','name':'synthetic_tool','arguments':'{}'}] if len(calls)==1 else [{'type':'message','content':[{'type':'output_text','text':'done'}]}]
                        return sse([{'type':'response.completed','response':{'status':'completed','output':output,'usage':{'input_tokens':100,'output_tokens':20}}}],done=False)
                    tool={'id':'call-1','type':'function','function':{'name':'synthetic_tool','arguments':'{}'}}
                    message={'content':'','tool_calls':[tool]} if len(calls)==1 else {'content':'done'}
                    return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls' if len(calls)==1 else 'stop','message':message}], 'usage':RAW})
                ns = provider_functions(handler)
                ns['RESPONSES_API_MODELS'] = {ROUTE['model']} if responses else set()
                legacy = SimpleNamespace(**{k:v for k,v in ns.items() if not k.startswith('__')})
                legacy.STREAM_OUTPUT=responses
                collector = self.make_collector(protocol='responses' if responses else 'chat',dialect='openai_responses' if responses else 'openai_chat')
                context=SimpleNamespace(definitions=lambda:[{'name':'synthetic_tool','description':'offline','parameters':{'type':'object'}}], invoke=AsyncMock(return_value='synthetic tool output'))
                sid='mcp-responses' if responses else 'mcp-chat'
                with capture_scope(collector,sid,'tool-turn'):
                    result = await mcp.run(legacy,ROUTE,[{'role':'system','content':'stable'}],context,AsyncMock())
                self.assertEqual(result['outcome'],'success'); self.assertEqual(len(calls),2)
                self.assertEqual(len(self.ledger.records('api',sid)),2); self.assertEqual(context.invoke.await_count,1)
                self.assertEqual(build_view(self.ledger,'api',sid)['last_round']['metrics']['input']['value'],200)

    async def test_invalid_profiles_and_conflicting_cache_fields_never_silently_override(self):
        with self.assertRaises(LedgerError): self.make_collector(cache=CachePolicy('openai_responses_implicit','30m'))
        with self.assertRaises(LedgerError): self.make_collector(dialect='deepseek_chat',cache=CachePolicy('openai_legacy'))
        with self.assertRaises(LedgerError): CachePolicy('openai_responses_implicit','24h')
        collector=self.make_collector(cache=CachePolicy('openai_legacy','in-memory'))
        with capture_scope(collector,'session-a','turn-a'):
            @capture.observe_api(protocol='chat')
            async def run(route):
                body={'messages':[], 'prompt_cache_key':'preexisting-explicit-key'}
                self.assertIs(capture.prepare_body(body),body)
            await run(ROUTE)
        self.assertEqual(collector.health()['configuration_errors'],1)
        self.assertEqual(self.ledger.records('api')[0]['cache_policy'],'configuration_unapplied')

    async def test_injected_web_runtime_capture_is_readable_from_authenticated_ledger_routes(self):
        from fastapi import FastAPI, HTTPException
        from backend.api_web_generation import ApiWebRuntime
        from backend.usage_ledger_routes import install
        relay = Path(self.ledger.path).parent/'synthetic-relay.sqlite'
        with sqlite3.connect(relay) as conn:
            conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
        async def handler(req):
            return sse([{'choices':[{'delta':{'content':'synthetic final reply'}}], 'usage':RAW}])
        ns = provider_functions(handler)
        legacy = SimpleNamespace(RELAY_DB=relay,run_model=ns['run_model'],
            build_ingest_messages=lambda text,**kw:[{'role':'system','content':'synthetic stable instructions'}, {'role':'user','content':text}],
            relay_out=AsyncMock(return_value=(True,{},False)))
        runtime=ApiWebRuntime(legacy,usage_collector=self.collector);runtime.start()
        self.addAsyncCleanup(runtime.close)
        row=runtime.store.accept('session-a','synthetic user text',{'api_session':'session-a','channel':'web','source':'relay','attachments':[]})
        await runtime.ingest({'id':row['id'],'text':row['text'],'session_id':'session-a'})
        await asyncio.gather(*list(runtime.tasks.values()))
        self.assertEqual(runtime.status('session-a')['generation']['status'],'completed')
        # A repeated admission never causes another model call or ledger entry.
        await runtime.ingest({'id':row['id'],'text':row['text'],'session_id':'session-a'})
        self.assertEqual(len(self.ledger.records('api')),1)
        app=FastAPI()
        def auth(request):
            if request.headers.get('authorization')!='Bearer synthetic-fixture': raise HTTPException(401)
        install(app,ledger=self.ledger,check_auth=auth,session_provider=lambda sid:'api' if sid=='session-a' else None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
            self.assertEqual((await client.get('/app/sessions/session-a/usage-ledger')).status_code,401)
            client.headers['Authorization']='Bearer synthetic-fixture'
            view=(await client.get('/app/sessions/session-a/usage-ledger')).json()
            self.assertEqual(view['summary']['metrics']['input']['value'],100)
            self.assertEqual(view['last_round']['metrics']['input']['value'],100)
            exported=(await client.get('/app/sessions/session-a/usage-ledger/export')).json()
            self.assertEqual(exported['records'][0]['raw_usage'],RAW)
            self.assertEqual(exported['records'][0]['turn_id'],'api-gen-'+str(row['id']))
            self.assertNotIn('synthetic user text',json.dumps(exported))
            self.assertNotIn('synthetic stable instructions',json.dumps(exported))
            self.assertNotIn(ROUTE['key'],json.dumps(exported))
            self.assertEqual((await client.get('/app/sessions/session-a/usage-ledger?mode=codex')).json()['summary']['requests'],0)
