"""Explicit status admission: unit ASGI plus full API-loop subprocess TCP."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
import httpx

from backend.session_lifecycle_http_interlock import install_api_loop_interlock, ALLOWED
from backend.session_lifecycle_history_http import PREFIX
from backend.session_window_activation_http import install_api_loop, PATH
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_activation_http import fixture
from backend.tests.test_session_lifecycle_history_http import original_auth,TOKEN
from backend.tests import test_session_lifecycle_history_tcp as tcp


@unittest.skipUnless(os.name=='posix','private canonical JSON fixture')
class ActivationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.h,self.journal,self.body=fixture(Path(temp.name)/'canonical.sqlite');self.addCleanup(self.h.conn.close)

    def app(self,*,status=True):
        app=FastAPI();loop=SimpleNamespace(app=app,check_internal_auth=original_auth())
        async def metadata():return {'metadata':True}
        for path in ALLOWED:app.add_api_route(path,metadata,methods=['POST'])
        app.add_api_route(PREFIX,metadata,methods=['GET'])
        if status:install_api_loop(loop,self.journal,access=CTX,enabled=True)
        return loop

    async def test_default_off_keeps_installed_status_blocked(self):
        loop=self.app();install_api_loop_interlock(loop,enabled=True,strict_reads=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=loop.app),base_url='http://synthetic.invalid') as c:
            r=await c.post(PATH,json=self.body,headers={'x-api-loop-internal-token':TOKEN})
            self.assertEqual(r.status_code,423)

    async def test_explicit_status_preserves_strict_reader_and_writer_refusal(self):
        loop=self.app();install_api_loop_interlock(loop,enabled=True,strict_reads=True,allow_activation_status=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=loop.app),base_url='http://synthetic.invalid',
                                   headers={'x-api-loop-internal-token':TOKEN}) as c:
            self.assertEqual((await c.post(PATH,json=self.body)).status_code,200)
            for method,path in [('GET',PATH),('HEAD',PATH),('POST','/loop/ingest'),('PATCH','/loop/config'),('GET','/loop/config')]:
                self.assertEqual((await c.request(method,path)).status_code,423)
            self.assertEqual((await c.post(PATH,json=self.body,headers={'x-api-loop-internal-token':'wrong'})).status_code,401)

    async def test_encoded_alias_and_trailing_slash_are_not_allowlisted(self):
        loop=self.app();install_api_loop_interlock(loop,enabled=True,strict_reads=True,allow_activation_status=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=loop.app),base_url='http://synthetic.invalid',
                                   headers={'x-api-loop-internal-token':TOKEN}) as c:
            for path in (PATH+'/',PATH.replace('/activation/','/activation%2f')):
                self.assertEqual((await c.post(path,json=self.body)).status_code,423)

    async def test_missing_installer_or_marker_rejects_without_middleware_mutation(self):
        loop=self.app(status=False)
        async def fake():return {}
        for fake_route in (False,True):
            if fake_route:loop.app.add_api_route(PATH,fake,methods=['POST'])
            with self.assertRaisesRegex(ValueError,'activation_status_missing'):
                install_api_loop_interlock(loop,enabled=True,strict_reads=True,allow_activation_status=True)
            self.assertEqual(loop.app.user_middleware,[])

    async def test_flag_type_strict_and_disabled_installer_is_inert(self):
        self.assertFalse(install_api_loop_interlock(None,allow_activation_status='yes'))
        loop=self.app()
        with self.assertRaisesRegex(ValueError,'binding_invalid'):
            install_api_loop_interlock(loop,enabled=True,allow_activation_status=1)
        self.assertEqual(loop.app.user_middleware,[])


@unittest.skipUnless(os.name=='posix','full app POSIX loopback process')
class FullActivationStatusTests(unittest.TestCase):
    def setUp(self):
        self.f=tcp.HistoryTcpTests();self.addCleanup(self.f.doCleanups);self.f.setUp();self.f.stop()
        self.h=self.f.f.h
        with closing(self.h.connect_canonical()) as conn:install_schema(conn)
        journal=WindowAttemptJournal(self.h.repo,enabled=True)
        self.h.engine.closeout(CID,'close_1',window_id='window_0',context_epoch=0,access=CTX)
        self.h.engine.reentry(CID,'prepare_1',closeout_id='close_1',target_window_id='window_1',access=CTX)
        state=self.h.record()
        journal.record_attempt(CID,'attempt_1',packet_sha256=state.packet_sha256,expected_revision=state.revision,access=CTX)
        self.h.confirm()
        activate_recorded_attempt(self.h.engine,journal,CID,'activate_1',attempt_request_id='attempt_1',
                                 packet_sha256=state.packet_sha256,access=CTX,enabled=True)
        self.body={'conversation_id':CID,'activation_request_id':'activate_1','attempt_request_id':'attempt_1',
                   'packet_sha256':state.packet_sha256}
        self.f.start('activation_status')

    def request(self,method,path,body=None,token=tcp.TOKEN):
        c=http.client.HTTPConnection('127.0.0.1',self.f.port,timeout=5)
        try:
            headers={'Content-Type':'application/json'}
            if token is not None:headers['x-api-loop-internal-token']=token
            c.request(method,path,None if body is None else json.dumps(body),headers)
            r=c.getresponse();raw=r.read();return r.status,json.loads(raw) if raw else {},dict(r.getheaders())
        finally:c.close()

    def test_full_lifespan_status_restart_and_legacy_rejections_are_side_effect_free(self):
        self.assertTrue(self.f.ready['activation_status_allowed'])
        self.assertTrue(self.f.ready['runtime_entrypoints_guarded'])
        before=list(self.h.conn.iterdump());config=self.h.config_path.read_bytes()
        self.assertEqual(self.request('POST',PATH,self.body,None)[0],401)
        for _ in range(2):
            status,value,headers=self.request('POST',PATH,self.body)
            self.assertEqual(status,200,value);self.assertEqual(value['state'],'historical_confirmed')
            self.assertFalse(value['current_window_confirmed']);self.assertFalse(value['retry_authorized'])
            self.assertEqual(headers['cache-control'],'private, no-store')
        for method,path in [('POST','/loop/ingest'),('PATCH','/loop/config'),('GET','/loop/config'),('GET','/healthz')]:
            self.assertEqual(self.request(method,path,{'text':'synthetic must not ingest'})[0],423)
        self.assertEqual(self.request('POST',PATH+'/',self.body)[0],423)
        self.assertEqual(list(self.h.conn.iterdump()),before);self.assertEqual(self.h.config_path.read_bytes(),config)
        self.assertEqual(self.f.f.generation(),0)
        self.f.stop();self.f.start('activation_status')
        self.assertEqual(self.request('POST',PATH,self.body)[1]['state'],'historical_confirmed')
        self.assertEqual(self.h.config_path.read_bytes(),config);self.assertEqual(self.f.f.generation(),0)

    def test_installed_status_stays_blocked_without_explicit_admission(self):
        self.f.stop();self.f.start('activation_status_blocked')
        self.assertFalse(self.f.ready['activation_status_allowed'])
        before=list(self.h.conn.iterdump());config=self.h.config_path.read_bytes()
        self.assertEqual(self.request('POST',PATH,self.body)[0],423)
        self.assertEqual(self.request('POST',PATH,self.body,None)[0],401)
        self.assertEqual(list(self.h.conn.iterdump()),before);self.assertEqual(self.h.config_path.read_bytes(),config)
