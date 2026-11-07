"""ASGI admission tests: rejected bodies and legacy handlers never run."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI,Request
import httpx
from backend.session_lifecycle_http_interlock import install_api_loop_interlock,HistoryHttpInterlock
from backend.session_lifecycle_history_http import install_api_loop,PREFIX
from backend.tests import test_session_lifecycle_history_http as web
from backend.tests.test_session_lifecycle_history_packet import binding


class HttpInterlockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=web.fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.a=binding(self.f.h.path,self.directory);self.app=FastAPI();self.effects=[]
        @self.app.post('/legacy')
        async def legacy():self.effects.append('write');return {'ok':True}
        @self.app.get('/read')
        async def read():return {'read':True}
        self.loop=SimpleNamespace(app=self.app,check_internal_auth=web.original_auth())
        install_api_loop(self.loop,self.a,principal_id='synthetic_owner',enabled=True)
        install_api_loop_interlock(self.loop,enabled=True)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic.invalid',
            headers={'x-api-loop-internal-token':web.TOKEN});self.addAsyncCleanup(self.client.aclose)
    async def test_default_off_does_not_touch_dependencies_or_install(self):
        self.assertFalse(install_api_loop_interlock(None))
    async def test_authenticated_legacy_write_refused_before_body_or_handler(self):
        with patch.object(Request,'stream',side_effect=AssertionError('body read')):
            r=await self.client.post('/legacy',content=b'x'*9000)
        self.assertEqual(r.status_code,423);self.assertEqual(r.json()['error'],'lifecycle_writer_blocked')
        self.assertEqual(self.effects,[]);self.assertEqual(r.headers['cache-control'],'private, no-store')
    async def test_wrong_or_duplicate_token_cannot_get_authorized_rejection(self):
        for headers in ({'x-api-loop-internal-token':'wrong'}, [('x-api-loop-internal-token',web.TOKEN),('x-api-loop-internal-token',web.TOKEN)]):
            r=await self.client.post('/legacy',headers=headers,content='not json')
            self.assertEqual(r.status_code,401)
        self.assertEqual(self.effects,[])
    async def test_candidate_prepare_execute_status_continue_through_original_auth(self):
        r=await self.client.post(PREFIX+'/prepare',json={'request_id':'gated','history_n':73})
        self.assertEqual(r.status_code,200,r.text)
        ref={k:r.json()[k] for k in ('request_id','packet_sha')}
        self.assertEqual((await self.client.post(PREFIX+'/execute',json={**ref,'recover':False})).status_code,200)
        self.assertEqual((await self.client.post(PREFIX+'/status',json=ref)).json()['state'],'committed')
        self.assertEqual(self.f.h.get('legacy')['history_n'],73)
    async def test_nonread_methods_and_encoded_candidate_alias_cannot_bypass(self):
        for method in ('PATCH','PUT','DELETE','TRACE','CONNECT'):
            r=await self.client.request(method,'/legacy');self.assertEqual(r.status_code,423)
        r=await self.client.post(PREFIX+'/%70repare',json={'request_id':'encoded','history_n':5})
        self.assertEqual(r.status_code,423);self.assertEqual(list(self.directory.iterdir()),[])
    async def test_read_method_passthrough_is_explicit_not_a_read_fence(self):
        r=await self.client.get('/read');self.assertEqual(r.json(),{'read':True})
    async def test_install_requires_candidates_and_rejects_duplicate_registration(self):
        with self.assertRaisesRegex(ValueError,'candidate_missing'):
            install_api_loop_interlock(SimpleNamespace(app=FastAPI(),check_internal_auth=web.original_auth()),enabled=True)
        count=len(self.app.user_middleware)
        with self.assertRaisesRegex(ValueError,'binding_invalid'):install_api_loop_interlock(self.loop,enabled=True)
        self.assertEqual(len(self.app.user_middleware),count)
    async def test_websocket_cannot_be_a_mutation_transport_in_restricted_mode(self):
        events=[]
        async def forbidden(*_):self.fail('websocket forwarded')
        async def receive():self.fail('websocket received')
        async def send(value):events.append(value)
        gate=HistoryHttpInterlock(forbidden,check_internal_auth=web.original_auth())
        await gate({'type':'websocket'},receive,send)
        self.assertEqual(events,[{'type':'websocket.close','code':1008}])


if __name__=='__main__':unittest.main()
