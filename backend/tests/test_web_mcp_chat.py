"""Synthetic action approvals and API function round trips; no external calls."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
from fastapi import FastAPI, HTTPException

from backend import web_mcp_chat as mcp, web_mcp_settings as settings, web_mcp_credentials as credentials
from backend import web_mcp_inspection as wire, web_mcp_chat_routes as routes
from backend.api_web_generation import ApiWebRuntime, Store as ApiStore, assert_idle
from backend import api_mcp_model as model
from backend.tests.test_web_mcp_inspection import ScriptedTransport

PLUGIN = {'id':'reader','serverId':'reader','name':'合成插件','transport':'streamable-http','enabled':True,
          'url':'https://example.com/mcp','options':{}}
TOOL = {'name':'lookup','description':'Synthetic fixture only','inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}}


class Fixture:
    def initialize(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'relay.db'
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
        self.api = ApiStore(self.path); self.api.initialize()
        self.store = mcp.Store(self.path)
        settings.save(self.path, {'revision':0,'plugins':[PLUGIN]})
        self.store.save('chat', {'revision':0,'config_revision':1,'plugin_ids':['reader']})
        row = self.api.accept('chat','synthetic',{'api_session':'chat','channel':'web','source':'relay'})
        self.mid = row['id']; self.api.claim('chat',self.mid,'synthetic')
        self.value = {'plugin':PLUGIN,'tool':TOOL,'config_revision':1,'credential_revision':0,
                      'selection_revision':1,'arguments':{'query':'fixture'}}
    def propose(self):
        return self.store.propose('chat',self.mid,self.value)
    def decide(self, aid, decision='approve'):
        return self.store.decision('chat',aid,{'revision':0,'decision':decision})


class DurableTests(Fixture, unittest.TestCase):
    def setUp(self): self.initialize()

    def test_default_off_and_explicit_selection_cas(self):
        with patch.object(wire,'discover') as network:
            self.assertIsNone(mcp.prepare(self.path,'unselected',self.mid)); network.assert_not_called()
        with self.assertRaises(settings.SettingsError): self.store.save('chat',{'revision':0,'config_revision':1,'plugin_ids':[]})
        with self.assertRaises(settings.SettingsError): self.store.save('chat',{'revision':1,'config_revision':1,'plugin_ids':['absent']})
        self.assertEqual(self.store.read('chat')['plugin_ids'],['reader'])
        settings.save(self.path,{'revision':1,'plugins':[PLUGIN]})
        self.assertTrue(self.store.read('chat')['stale'])

    def test_approval_one_winner_and_session_binding(self):
        aid=self.propose();results=[]
        def approve():
            try: self.decide(aid);results.append('ok')
            except settings.SettingsError: results.append('conflict')
        threads=[threading.Thread(target=approve) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results,['ok','conflict'])
        with self.assertRaises(settings.SettingsError): self.store.decision('other',aid,{'revision':0,'decision':'approve'})
        self.store.claim(aid)
        with self.assertRaises(mcp.McpError): self.store.claim(aid)

    def test_regenerated_alternatives_share_selection_but_not_actions(self):
        with self.store.db() as conn:
            conn.execute('CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY,root_id TEXT)')
            conn.execute("INSERT INTO web_message_versions VALUES ('answer2','chat')")
        self.assertEqual(self.store.read('answer2')['plugin_ids'],['reader'])
        self.store.save('answer2',{'revision':1,'config_revision':1,'plugin_ids':[]})
        self.assertEqual(self.store.read('chat')['plugin_ids'],[])
        self.assertEqual(self.store.read('chat')['revision'],2)
        with self.assertRaises(settings.SettingsError):self.store.save('chat',{'revision':1,'config_revision':1,'plugin_ids':['reader']})
        self.assertEqual(self.store.actions('answer2')['actions'],[])

    def test_changed_selection_config_credentials_block_dispatch(self):
        for change in ('selection','config','credentials'):
            with self.subTest(change=change):
                aid=self.propose();self.decide(aid)
                if change=='selection': self.store.save('chat',{'revision':1,'config_revision':1,'plugin_ids':[]})
                elif change=='config': settings.save(self.path,{'revision':1,'plugins':[{**PLUGIN,'url':'https://example.org/mcp'}]})
                else: credentials.write(self.path,'reader',{'revision':1,'credential_revision':0,'bearer_token':'synthetic-token'})
                with self.assertRaises((settings.SettingsError,mcp.McpError)): self.store.claim(aid)
                # Reset isolated fixture for each mutation without accessing any real DB.
                self.initialize()

    def test_restart_and_stop_never_replay_and_delete_removes_actions(self):
        pending=self.propose();executing=self.propose();self.decide(executing);self.store.claim(executing)
        self.store.recover()
        self.assertEqual(self.store.state(pending)['status'],'cancelled')
        self.assertEqual(self.store.state(executing)['status'],'unknown')
        self.store.mark(executing,'completed','late response')
        self.assertEqual(self.store.state(executing)['status'],'unknown')
        self.api.stop('chat',self.mid);self.api.finish('chat',self.mid,status='interrupted')
        with self.api.db() as conn: assert_idle(conn,'chat',deleting=True)
        self.assertEqual(self.store.actions('chat')['actions'],[])
        self.assertEqual(self.store.read('chat')['plugin_ids'],[])

    def test_expiry_and_bounded_self_contained_schemas(self):
        aid=self.propose()
        with self.store.db() as conn: conn.execute('UPDATE web_mcp_actions SET expires_at=0 WHERE id=?',(aid,))
        with self.assertRaises(settings.SettingsError): self.decide(aid)
        self.assertEqual(self.store.actions('chat')['actions'][0]['status'],'expired')
        for key in ('$ref','$dynamicRef','pattern','x-mcp-header'):
            self.assertFalse(mcp.schema_ok({'type':'object',key:'https://localhost/secret'}))
        self.assertTrue(mcp.schema_ok(TOOL['inputSchema']))

    def test_fresh_modern_and_legacy_tool_schema_before_single_call(self):
        for legacy in (False,True):
            responses = ([(400,None,None),(200,{'result':{'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}},'session'),(202,None,None)] if legacy else [])
            responses += [(200,{'result':{'tools':[TOOL]}},None),(200,{'result':{'content':[{'type':'text','text':'fixture'}]}},None)]
            transport=ScriptedTransport(responses);claim=Mock()
            value=wire.discover(PLUGIN,lambda *_:transport,include_schemas=True,call=(TOOL,{'query':'fixture'}),before_call=claim)
            self.assertEqual(value['content'][0]['text'],'fixture');claim.assert_called_once()
            self.assertEqual(sum(c[0] is not None and c[0]['method']=='tools/call' for c in transport.calls),1)
        transport=ScriptedTransport([(200,{'result':{'tools':[{**TOOL,'inputSchema':{'type':'object'}}]}},None)])
        claim=Mock()
        with self.assertRaises(wire.InspectionError): wire.discover(PLUGIN,lambda *_:transport,include_schemas=True,call=(TOOL,{}),before_call=claim)
        claim.assert_not_called()


class AsyncTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): self.initialize()
    async def wait_action(self):
        for _ in range(100):
            values=self.store.actions('chat')['actions']
            if values:return values[-1]
            await asyncio.sleep(.01)
        self.fail('action not proposed')

    async def test_denial_and_cancellation_have_no_external_call(self):
        context=mcp.Context(self.store,'chat',self.mid,{'alias':{k:v for k,v in self.value.items() if k!='arguments'}})
        with patch.object(wire,'discover') as network:
            task=asyncio.create_task(context.invoke('alias',{'query':'fixture'}));row=await self.wait_action()
            self.decide(row['id'],'deny')
            self.assertIn('denied',await task);network.assert_not_called()
            with self.store.db() as conn:conn.execute('DELETE FROM web_mcp_actions')
            task=asyncio.create_task(context.invoke('alias',{'query':'fixture'}));row=await self.wait_action()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(self.store.state(row['id'])['status'],'cancelled');network.assert_not_called()

    async def test_ambiguous_call_is_recorded_and_never_retried(self):
        aid=self.propose();self.decide(aid)
        def fail(*args,**kwargs):kwargs['before_call']();raise OSError('private remote error')
        context=mcp.Context(self.store,'chat',self.mid,{})
        with patch.object(wire,'discover',side_effect=fail) as network:
            with self.assertRaises(mcp.McpError):await asyncio.to_thread(context.execute,aid,self.value)
            self.assertEqual(self.store.state(aid)['status'],'unknown');self.assertEqual(network.call_count,1)
            with self.assertRaises(settings.SettingsError):self.decide(aid)
        self.assertNotIn('private',json.dumps(self.store.actions('chat')))

    async def test_chat_completions_and_responses_continue_after_confirmation(self):
        for responses in (False,True):
            with self.subTest(responses=responses):
                await self.model_flow(responses)
                self.initialize()

    async def model_flow(self,responses):
        requests,invoked,chunks=[],[],[]
        secret='synthetic-"quoted"-header'
        credentials.write(self.path,'reader',{'revision':1,'credential_revision':0,'http_headers':{'X-Api-Key':secret}})
        def discover(plugin,**kwargs):
            self.assertEqual(kwargs['http_headers'],{'x-api-key':secret})
            if kwargs.get('call') is None:return {'tools':[TOOL],'truncated':False,'protocol_version':wire.MODERN}
            kwargs['before_call']();invoked.append(kwargs['call'])
            return {'content':[{'type':'text','text':'合成结果 <img onerror=bad> '+secret}], 'structuredContent':{'key':secret}}
        async def handler(request):
            data=json.loads(request.content);requests.append(data)
            if len(requests)==1:
                alias=(data['tools'][0] if responses else data['tools'][0]['function'])['name']
                call={'type':'function_call','call_id':'call1','name':alias,'arguments':'{"query":"fixture"}'}
                if responses:
                    output=[{'type':'reasoning','id':'r1','encrypted_content':'synthetic','summary':[]},call]
                    payload=[{'type':'response.completed','response':{'status':'completed','output':output,'usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12}}}]
                else:
                    payload=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call1','type':'function','function':{'name':alias,'arguments':'{"query":'}}]},'finish_reason':None}]},
                             {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"fixture"}'}}]},'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}]
            elif responses:
                self.assertTrue(any(v.get('type')=='reasoning' for v in data['input']))
                self.assertIn('合成结果',next(v['output'] for v in data['input'] if v.get('type')=='function_call_output'))
                payload=[{'type':'response.output_text.delta','delta':'完成回复'},{'type':'response.completed','response':{'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'完成回复'}]}],'usage':{'input_tokens':20,'output_tokens':3,'total_tokens':23}}}]
            else:
                self.assertEqual(data['messages'][-1]['role'],'tool');self.assertIn('合成结果',data['messages'][-1]['content'])
                payload=[{'choices':[{'delta':{'content':'完成回复'},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':3,'total_tokens':23}}]
            body=''.join('data: '+json.dumps(p,ensure_ascii=False)+'\n\n' for p in payload)+('' if responses else 'data: [DONE]\n\n')
            return httpx.Response(200,headers={'Content-Type':'text/event-stream'},text=body)
        legacy=SimpleNamespace(RELAY_DB=self.path,STREAM_OUTPUT=True,LOOP_ASSISTANT_MAX_CHARS=64000,LOOP_MODEL_TOTAL_TIMEOUT_SECONDS=2,
            main_chain=lambda:[{'url':'https://synthetic.invalid/v1','key':'synthetic-api-key','model':'fixture'}],
            _uses_responses_api=lambda _:responses,_responses_body=lambda r,m,**kw:{'model':r['model'],'input':m,**kw},
            _chat_completion_body=lambda r,m,**kw:{'model':r['model'],'messages':m,**kw},
            _responses_text=lambda _: '',_check_provider_response=AsyncMock(),_provider_client=lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            run_model=AsyncMock(side_effect=AssertionError('no fallback')))
        async def sink(chunk):chunks.append(chunk)
        with patch.object(wire,'discover',side_effect=discover):
            task=asyncio.create_task(model.generate(legacy,'chat',self.mid,[{'role':'user','content':'synthetic'}],sink,lambda _:None))
            row=await self.wait_action();self.assertEqual(len(requests),1);self.assertEqual(invoked,[])
            self.assertEqual(row['arguments'],{'query':'fixture'});self.decide(row['id'])
            result=await asyncio.wait_for(task,4)
        self.assertEqual(result['outcome'],'success');self.assertEqual(result['usage']['total_tokens'],35)
        self.assertTrue(result['usage_complete']);self.assertEqual(''.join(chunks),'完成回复')
        self.assertEqual(len(invoked),1);self.assertEqual(len(requests),2)
        self.assertEqual(self.store.actions('chat')['actions'][0]['status'],'completed')
        self.assertNotIn('synthetic-api-key',json.dumps(self.store.actions('chat')))
        self.assertNotIn(secret,json.dumps(self.store.actions('chat')));self.assertNotIn('quoted',json.dumps(requests))
        self.assertIn('[redacted]',json.dumps(requests))

    async def test_authenticated_routes_limits_provider_and_stale_decision(self):
        app=FastAPI()
        def auth(request):
            if request.headers.get('Authorization')!='Bearer fixture':raise HTTPException(401)
        integration=SimpleNamespace(_session_lock=threading.RLock(),session_authority=SimpleNamespace(row_for_session=lambda sid:None if sid=='missing' else {'provider':'codex' if sid=='codex' else 'api'}))
        routes.install_loop(app,SimpleNamespace(RELAY_DB=self.path,check_internal_auth=auth),integration)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            self.assertEqual((await client.get('/loop/sessions/chat/mcp')).status_code,401)
            client.headers['Authorization']='Bearer fixture'
            value=await client.get('/loop/sessions/chat/mcp');self.assertIn('no-store',value.headers['Cache-Control'])
            self.assertEqual(value.json()['plugin_ids'],['reader'])
            self.assertFalse((await client.get('/loop/sessions/codex/mcp')).json()['supported'])
            self.assertEqual((await client.put('/loop/sessions/codex/mcp',json={})).status_code,409)
            self.assertEqual((await client.get('/loop/sessions/missing/mcp')).status_code,404)
            self.assertEqual((await client.put('/loop/sessions/chat/mcp',content='x'*8193)).status_code,413)
            aid=self.propose()
            self.assertEqual((await client.post(f'/loop/sessions/chat/mcp/actions/{aid}',json={'revision':0,'decision':'deny'})).status_code,200)
            self.assertEqual((await client.post(f'/loop/sessions/chat/mcp/actions/{aid}',json={'revision':0,'decision':'approve'})).status_code,409)
