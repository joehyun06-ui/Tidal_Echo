"""Real FastAPI/ASGI requests, original internal auth, private SQLite/JSON."""
import ast
import hmac
import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
import httpx
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_history_http import install,install_api_loop,PREFIX
from backend.session_lifecycle_history_packet import PrivateHistorySetting
from backend.session_lifecycle_request_packets import PrivateCommandPackets
from backend.session_lifecycle_coordination import digest,encode
from backend.tests import test_session_lifecycle_model_commands as fixtures
from backend.tests.test_session_lifecycle_commands import commands
from backend.tests.test_session_lifecycle_create_delete_packets import pinned

TOKEN='synthetic-internal-token'


def original_auth():
    path=Path(__file__).resolve().parents[2]/'examples'/'api_loop.py'
    node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='check_internal_auth')
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    scope={'hmac':hmac,'HTTPException':HTTPException,'API_LOOP_INTERNAL_TOKEN':TOKEN}
    exec(compile(ast.fix_missing_locations(module),str(path),'exec'),scope)
    return scope['check_internal_auth']


class HistoryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.owner='synthetic_owner';self.allowed=True;self.storage_allowed=True
        self.check=original_auth();self.access_calls=0
        self.app,self.a=self.build()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic.invalid',
            headers={'x-api-loop-internal-token':TOKEN})
        self.addAsyncCleanup(self.client.aclose)
    def build(self):
        c=pinned(self.f.h.path);lock=RLock()
        w=commands(c,lock=lock,authorize=lambda *_:self.allowed)
        store=PrivateCommandPackets(self.directory,binding=digest(encode([str(c.canonical_path),str(c.config),str(c.directory)])),
            authorize=lambda *_:self.storage_allowed,enabled=True)
        a=PrivateHistorySetting(w,store,session_lock=lock,enabled=True)
        def authenticate(request):
            self.check(request);self.access_calls+=1
            return ConfigCommandAccess(self.owner)
        app=FastAPI();install(app,a,authenticate=authenticate,enabled=True);return app,a
    async def prepare(self,n=73,rid='http-history'):
        return await self.client.post(PREFIX+'/prepare',json={'history_n':n,'request_id':rid})
    async def ref(self):
        r=await self.prepare();self.assertEqual(r.status_code,200,r.text)
        return {k:r.json()[k] for k in ('request_id','packet_sha')}
    async def execute(self,ref,recover=False):
        return await self.client.post(PREFIX+'/execute',json={**ref,'recover':recover})
    def assert_private(self,r):
        self.assertEqual(r.headers['cache-control'],'private, no-store')
        self.assertEqual(r.headers['referrer-policy'],'no-referrer')
        self.assertNotIn(fixtures.ROUTE['key'],r.text);self.assertNotIn(str(self.f.root),r.text)
    async def test_default_install_does_not_register_or_touch_dependencies(self):
        app=FastAPI();self.assertFalse(install(app,None,authenticate=None))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid') as c:
            self.assertEqual((await c.post(PREFIX+'/prepare')).status_code,404)
    async def test_prepare_then_execute_and_replay_are_real_http_and_idempotent(self):
        raw=self.f.h.config_path.read_bytes();ref=await self.ref();self.assertEqual(raw,self.f.h.config_path.read_bytes())
        self.assertEqual(self.f.generation(),0)
        first=await self.execute(ref);second=await self.execute(ref)
        self.assertEqual(first.json(),{'ok':True,'generation':1,'replayed':False})
        self.assertEqual(second.json(),{'ok':True,'generation':1,'replayed':True})
        self.assertEqual(self.f.h.get('legacy')['history_n'],73);self.assert_private(first)
    async def test_real_internal_auth_runs_before_body_or_private_io(self):
        self.a.packets.directory=Path('/unavailable')
        r=await self.client.post(PREFIX+'/prepare',content='not json',headers={'x-api-loop-internal-token':'wrong'})
        self.assertEqual(r.status_code,401);self.assertEqual(self.access_calls,0);self.assert_private(r)
    async def test_query_cookie_and_body_cannot_supply_authentication(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic.invalid') as c:
            r=await c.post(PREFIX+'/prepare?token='+TOKEN,headers={'cookie':'token='+TOKEN},json={'token':TOKEN})
            self.assertEqual(r.status_code,401);self.assertEqual(self.access_calls,0)
    async def test_duplicate_auth_headers_refuse(self):
        r=await self.client.post(PREFIX+'/prepare',headers=[('x-api-loop-internal-token',TOKEN),('x-api-loop-internal-token',TOKEN)],json={})
        self.assertEqual(r.status_code,401)
    async def test_client_principal_access_recover_and_dispatch_fields_refuse(self):
        for extra in ({'principal_id':'admin'},{'access':{}},{'recover':True},{'method':'update_config'},{'main_chain':[]}):
            r=await self.client.post(PREFIX+'/prepare',json={'history_n':3,'request_id':'x',**extra})
            self.assertEqual(r.status_code,400,r.text)
        self.assertEqual(list(self.directory.iterdir()),[])
    async def test_duplicate_json_keys_and_invalid_count_refuse(self):
        r=await self.client.post(PREFIX+'/prepare',content='{"request_id":"x","history_n":1,"history_n":2}')
        self.assertEqual(r.status_code,400)
        for n in (True,-1,201,'7'):
            self.assertEqual((await self.prepare(n)).status_code,400)
    async def test_size_and_content_encoding_are_bounded(self):
        r=await self.client.post(PREFIX+'/prepare',content=b'x'*4097);self.assertEqual(r.status_code,413)
        r=await self.client.post(PREFIX+'/prepare',content=b'x',headers={'content-encoding':'gzip'})
        self.assertEqual(r.status_code,415);self.assertEqual(list(self.directory.iterdir()),[])
    async def test_prepare_only_recover_never_initiates_write(self):
        r=await self.execute(await self.ref(),True);self.assertEqual(r.status_code,409)
        self.assertEqual(self.f.generation(),0)
    async def test_current_command_and_storage_authorization_rechecked(self):
        ref=await self.ref();self.allowed=False
        self.assertEqual((await self.execute(ref)).status_code,403);self.allowed=True;self.storage_allowed=False
        self.assertEqual((await self.execute(ref)).status_code,403);self.assertEqual(self.f.generation(),0)
    async def test_new_authenticated_actor_cannot_recover_old_packet(self):
        ref=await self.ref();self.owner='different_owner'
        r=await self.execute(ref);self.assertEqual(r.status_code,409);self.assertEqual(self.f.generation(),0)
    async def test_reference_integrity_and_recover_bool_are_strict(self):
        ref=await self.ref();r=await self.execute({**ref,'packet_sha':'0'*64})
        self.assertEqual(r.status_code,409)
        r=await self.execute(ref,1);self.assertEqual(r.status_code,400)
    async def test_lost_execute_response_reopen_and_explicit_original_recovery(self):
        ref=await self.ref();r=await self.execute(ref);self.assertEqual(r.status_code,200)
        # Discard successful response as if transport lost it; keep original reference.
        app,_=self.build()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid',headers={'x-api-loop-internal-token':TOKEN}) as c:
            r=await c.post(PREFIX+'/execute',json={**ref,'recover':True})
        self.assertEqual(r.json(),{'ok':True,'generation':1,'replayed':True});self.assertEqual(self.f.generation(),1)
    async def test_pending_failure_reopens_same_ref_without_reapplying_mutation(self):
        ref=await self.ref()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('private secret/path')):
            r=await self.execute(ref)
        self.assertEqual(r.status_code,503);self.assertEqual(r.json()['error'],'outcome_unconfirmed');self.assert_private(r)
        app,_=self.build()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid',headers={'x-api-loop-internal-token':TOKEN}) as c:
            with patch('backend.session_lifecycle_commands.updated_config',side_effect=AssertionError('reapply')):
                r=await c.post(PREFIX+'/execute',json={**ref,'recover':True})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.f.generation(),1)
    async def test_historical_recovery_does_not_override_later_setting(self):
        ref=await self.ref();await self.execute(ref)
        self.a.commands.update_config('later',{'history_n':5},access=ConfigCommandAccess(self.owner))
        r=await self.execute(ref,True);self.assertEqual(r.status_code,200)
        self.assertEqual(self.f.h.get('legacy')['history_n'],5)
    async def test_duplicate_install_refuses_before_replacing_routes(self):
        before=len(self.app.routes)
        with self.assertRaisesRegex(ValueError,'route_conflict'):install(self.app,self.a,authenticate=lambda *_:None,enabled=True)
        self.assertEqual(len(self.app.routes),before)
    async def test_api_loop_assembly_uses_original_guard_and_server_owner(self):
        app=FastAPI();loop=SimpleNamespace(app=app,check_internal_auth=self.check)
        self.assertFalse(install_api_loop(None,None,principal_id=None))
        self.assertTrue(install_api_loop(loop,self.a,principal_id=self.owner,enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid') as c:
            body={'request_id':'server-owner','history_n':8}
            self.assertEqual((await c.post(PREFIX+'/prepare',json=body)).status_code,401)
            r=await c.post(PREFIX+'/prepare',json=body,headers={'x-api-loop-internal-token':TOKEN})
            self.assertEqual(r.status_code,200,r.text)
            value=json.loads(next(p for p in self.directory.glob('*.json') if 'server-owner' in p.read_text()).read_text())
            self.assertEqual(value['principal'],self.owner)


if __name__=='__main__':unittest.main()
