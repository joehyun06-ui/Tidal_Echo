"""Real ASGI and loopback TCP, original internal auth, synthetic data only."""
from contextlib import closing
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import uvicorn

from backend.g3_projection_contract import AccessScope
from backend.session_window_activation_http import install, install_api_loop, PATH
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_history_http import original_auth, TOKEN


def fixture(path):
    h=FileHarness(path)
    with closing(h.connect_canonical()) as conn:install_schema(conn)
    journal=WindowAttemptJournal(h.repo,enabled=True)
    h.engine.closeout(CID,'close_1',window_id='window_0',context_epoch=0,access=CTX)
    h.engine.reentry(CID,'prepare_1',closeout_id='close_1',target_window_id='window_1',access=CTX)
    state=h.record()
    body={'conversation_id':CID,'activation_request_id':'activate_1','attempt_request_id':'attempt_1',
          'packet_sha256':state.packet_sha256}
    return h,journal,body


@unittest.skipUnless(os.name=='posix','private JSON fixture uses POSIX')
class ActivationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.h,self.journal,self.body=fixture(Path(tmp.name)/'canonical.sqlite');self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')))
        self.app=FastAPI();self.check=original_auth();self.access=CTX
        def auth(request):self.check(request);return self.access
        install(self.app,self.journal,authenticate=auth,enabled=True)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic.invalid',
            headers={'x-api-loop-internal-token':TOKEN});self.addAsyncCleanup(self.client.aclose)

    def record(self):
        self.journal.record_attempt(CID,'attempt_1',packet_sha256=self.body['packet_sha256'],
            expected_revision=self.h.record().revision,access=CTX)

    def activate(self):
        self.h.confirm()
        activate_recorded_attempt(self.h.engine,self.journal,CID,'activate_1',attempt_request_id='attempt_1',
            packet_sha256=self.body['packet_sha256'],access=CTX,enabled=True)

    def private(self,response):
        self.assertEqual(response.headers['cache-control'],'private, no-store')
        self.assertEqual(response.headers['referrer-policy'],'no-referrer')
        self.assertNotIn(str(self.h.config_path),response.text)
        self.assertNotIn('synthetic original message',response.text)

    async def test_default_install_does_not_touch_dependencies(self):
        app=FastAPI();self.assertFalse(install(app,None,authenticate=None))
        self.assertFalse(install_api_loop(None,None,access=None))
        self.assertFalse(any(getattr(r,'path',None)==PATH for r in app.routes))

    async def test_three_historical_states_preserve_rows_and_never_retry(self):
        r=await self.client.post(PATH,json=self.body);self.assertEqual(r.json()['state'],'attempt_missing')
        self.record();r=await self.client.post(PATH,json=self.body);self.assertEqual(r.json()['state'],'unconfirmed')
        self.activate();before=list(self.h.conn.iterdump());config=self.h.config_path.read_bytes()
        for _ in range(2):
            r=await self.client.post(PATH,json=self.body);self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['state'],'historical_confirmed');self.assertFalse(r.json()['retry_authorized'])
            self.assertFalse(r.json()['current_window_confirmed']);self.private(r)
        self.assertEqual(list(self.h.conn.iterdump()),before);self.assertEqual(self.h.config_path.read_bytes(),config)

    async def test_auth_precedes_stream_or_storage(self):
        async def forbidden():
            raise AssertionError('must not read rejected body')
            yield b''
        with patch('backend.session_window_activation_http.inspect_recorded_activation',side_effect=AssertionError('no IO')):
            r=await self.client.post(PATH,content=forbidden(),headers={'x-api-loop-internal-token':'wrong'})
        self.assertEqual(r.status_code,401);self.private(r)

    async def test_cookie_query_and_body_cannot_grant_access(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic.invalid') as c:
            r=await c.post(PATH+'?token='+TOKEN,headers={'cookie':'token='+TOKEN},json={'token':TOKEN})
            self.assertEqual(r.status_code,401);self.private(r)

    async def test_duplicate_auth_headers_rejected(self):
        r=await self.client.post(PATH,json=self.body,headers=[('x-api-loop-internal-token',TOKEN)]*2)
        self.assertEqual(r.status_code,401)

    async def test_extra_dispatch_recover_principal_scope_fields_rejected(self):
        for key,value in [('recover',True),('dispatch',True),('principal_id','admin'),('allowed_scopes',['private_A'])]:
            r=await self.client.post(PATH,json={**self.body,key:value})
            self.assertEqual(r.status_code,400);self.private(r)

    async def test_query_and_duplicate_json_keys_rejected(self):
        self.assertEqual((await self.client.post(PATH+'?recover=true',json=self.body)).status_code,400)
        r=await self.client.post(PATH,content='{"conversation_id":"a","conversation_id":"b"}')
        self.assertEqual(r.status_code,400)

    async def test_large_body_and_encoding_rejected(self):
        self.assertEqual((await self.client.post(PATH,content=b'x'*4097)).status_code,413)
        r=await self.client.post(PATH,json=self.body,headers={'content-encoding':'gzip'})
        self.assertEqual(r.status_code,415);self.private(r)

    async def test_wrong_reference_maps_to_conflict(self):
        self.record()
        r=await self.client.post(PATH,json={**self.body,'packet_sha256':'0'*64})
        self.assertEqual(r.status_code,409);self.private(r)

    async def test_changed_server_owner_or_scope_is_denied(self):
        self.record();self.access=AccessScope('other_owner',CTX.allowed_scopes)
        self.assertEqual((await self.client.post(PATH,json=self.body)).status_code,403)
        self.access=AccessScope(CTX.principal_id,frozenset())
        self.assertEqual((await self.client.post(PATH,json=self.body)).status_code,403)

    async def test_corrupt_store_is_unavailable_without_private_details(self):
        self.record();self.h.conn.execute('UPDATE session_window_attempts SET record_sha256=?',('0'*64,))
        r=await self.client.post(PATH,json=self.body)
        self.assertEqual(r.status_code,503);self.assertEqual(r.json()['error'],'status_unavailable');self.private(r)

    async def test_auth_is_current_not_cached(self):
        self.check.__globals__['API_LOOP_INTERNAL_TOKEN']='rotated-synthetic-token'
        self.assertEqual((await self.client.post(PATH,json=self.body)).status_code,401)

    async def test_api_loop_wrapper_uses_original_guard_and_server_scope(self):
        app=FastAPI();loop=SimpleNamespace(app=app,check_internal_auth=original_auth())
        self.assertTrue(install_api_loop(loop,self.journal,access=CTX,enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid') as c:
            self.assertEqual((await c.post(PATH,json=self.body)).status_code,401)
            r=await c.post(PATH,json=self.body,headers={'x-api-loop-internal-token':TOKEN})
            self.assertEqual(r.status_code,200,r.text)

    async def test_duplicate_or_poststartup_install_does_not_add_route(self):
        before=len(self.app.routes)
        with self.assertRaisesRegex(ValueError,'route_conflict'):
            install(self.app,self.journal,authenticate=lambda _:CTX,enabled=True)
        await self.client.post(PATH,json=self.body)
        with self.assertRaisesRegex(ValueError,'binding_invalid'):
            install(self.app,self.journal,authenticate=lambda _:CTX,enabled=True)
        self.assertEqual(len(self.app.routes),before)


@unittest.skipUnless(os.name=='posix','private JSON fixture uses POSIX')
class ActivationTcpTests(unittest.TestCase):
    def test_real_uvicorn_tcp_status_only_keeps_canonical_data(self):
        with tempfile.TemporaryDirectory() as directory:
            h,journal,body=fixture(Path(directory)/'canonical.sqlite')
            self.addCleanup(h.conn.close)
            journal.record_attempt(CID,'attempt_1',packet_sha256=body['packet_sha256'],expected_revision=h.record().revision,access=CTX)
            h.confirm();activate_recorded_attempt(h.engine,journal,CID,'activate_1',attempt_request_id='attempt_1',
                packet_sha256=body['packet_sha256'],access=CTX,enabled=True)
            app=FastAPI();install_api_loop(SimpleNamespace(app=app,check_internal_auth=original_auth()),journal,access=CTX,enabled=True)
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16)
            server=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False,loop='asyncio',ws='none'))
            thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
            connect=socket.socket.connect;resolve=socket.getaddrinfo
            def local_connect(sock,address):
                if not isinstance(address,tuple) or address[0]!='127.0.0.1':raise AssertionError('loopback-only')
                return connect(sock,address)
            def local_resolve(host,*args,**kwargs):
                if host!='127.0.0.1':raise AssertionError('loopback-only')
                return resolve(host,*args,**kwargs)
            try:
                thread.start();deadline=time.monotonic()+10
                while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(server.started)
                before=list(h.conn.iterdump());config=h.config_path.read_bytes()
                with patch.object(socket.socket,'connect',local_connect),patch('socket.getaddrinfo',local_resolve):
                    with httpx.Client(base_url='http://127.0.0.1:'+str(listener.getsockname()[1]),trust_env=False,timeout=5) as c:
                        self.assertEqual(c.post(PATH,json=body).status_code,401)
                        for _ in range(2):
                            r=c.post(PATH,json=body,headers={'x-api-loop-internal-token':TOKEN})
                            self.assertEqual(r.status_code,200,r.text)
                            self.assertEqual(r.json()['state'],'historical_confirmed')
                            self.assertFalse(r.json()['retry_authorized'])
                            self.assertEqual(r.headers['cache-control'],'private, no-store')
                self.assertEqual(list(h.conn.iterdump()),before);self.assertEqual(h.config_path.read_bytes(),config)
            finally:
                server.should_exit=True;thread.join(10);listener.close()
                self.assertFalse(thread.is_alive())
