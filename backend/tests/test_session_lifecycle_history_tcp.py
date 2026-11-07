"""Actual TCP, full API-loop app import/lifespan and original header auth."""
import http.client
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from backend.tests import test_session_lifecycle_model_commands as fixtures
from backend.tests._session_lifecycle_history_tcp_server import TOKEN

PREFIX='/loop/lifecycle/history'


@unittest.skipUnless(os.name=='posix','private POSIX loopback integration')
class HistoryTcpTests(unittest.TestCase):
    def setUp(self):
        connect=socket.socket.connect;dns=socket.getaddrinfo
        self.f=fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        def local_connect(sock,address):
            if not isinstance(address,tuple) or address[0]!='127.0.0.1':raise AssertionError('external_connect_blocked')
            return connect(sock,address)
        def local_dns(host,*a,**kw):
            if host!='127.0.0.1':raise AssertionError('external_dns_blocked')
            return dns(host,*a,**kw)
        self.enterContext(patch.object(socket.socket,'connect',local_connect))
        self.enterContext(patch('socket.getaddrinfo',local_dns))
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.process=None;self.start()
    def stop(self):
        if self.process is None:return
        p=self.process;self.process=None
        p.terminate()
        try:out,err=p.communicate(timeout=5)
        except subprocess.TimeoutExpired:p.kill();p.communicate(timeout=5);self.fail('test_server_shutdown_timeout')
        self.assertEqual(p.returncode,0,err[-1500:])
        self.assertIn('"lifespan": "stopped"',out)
    def start(self,mode='normal'):
        self.process=subprocess.Popen([sys.executable,'-m','backend.tests._session_lifecycle_history_tcp_server',
            str(self.f.h.path),str(self.directory),mode],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout,selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10),'server readiness timeout')
            line=self.process.stdout.readline()
        self.assertTrue(line,'server exited before readiness')
        ready=json.loads(line);self.ready=ready;self.port=ready['port']
        self.assertEqual(ready['application'],'examples.api_loop');self.assertEqual(ready['lifespan'],'started')
        self.assertTrue(ready['external_connect_blocked'])
    def request(self,suffix,body,token=TOKEN):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            headers={'Content-Type':'application/json'}
            if token is not None:headers['x-api-loop-internal-token']=token
            c.request('POST',PREFIX+suffix,json.dumps(body),headers)
            r=c.getresponse();return r.status,json.loads(r.read()),dict(r.getheaders())
        finally:c.close()
    def ref(self):
        status,value,_=self.request('/prepare',{'request_id':'tcp-history','history_n':73})
        self.assertEqual(status,200,value)
        return {k:value[k] for k in ('request_id','packet_sha')}
    def test_default_disabled_candidate_has_no_routes_in_full_app(self):
        self.stop();self.start('disabled')
        self.assertEqual(self.request('/prepare',{})[0],404)
        self.assertEqual(self.request('/status',{})[0],404)
        self.assertEqual(self.f.generation(),0)
    def test_actual_internal_auth_precedes_invalid_body_and_has_no_effect(self):
        for token in (None,'wrong'):
            status,value,headers=self.request('/prepare',{'invalid':True},token)
            self.assertEqual(status,401);self.assertEqual(headers['cache-control'],'private, no-store')
        self.assertEqual(list(self.directory.iterdir()),[]);self.assertEqual(self.f.generation(),0)
    def test_prepare_execute_restart_status_and_original_recovery(self):
        ref=self.ref();self.assertEqual(self.f.generation(),0)
        self.assertEqual(self.request('/execute',{**ref,'recover':False})[0],200)
        self.stop();self.start()
        self.assertEqual(self.request('/status',ref)[1],{'ok':True,'state':'committed','generation':1,'historical':True})
        self.assertEqual(self.request('/execute',{**ref,'recover':True})[1]['replayed'],True)
        self.assertEqual(self.f.generation(),1)
    def test_discarded_response_does_not_need_new_request_id(self):
        ref=self.ref();body=json.dumps({**ref,'recover':False}).encode()
        raw=(f'POST {PREFIX}/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nX-API-Loop-Internal-Token: {TOKEN}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body
        with socket.create_connection(('127.0.0.1',self.port),timeout=5) as c:
            c.sendall(raw)
            # Test oracle waits for durable server commit, deliberately never reads
            # the HTTP response. This is response loss, not unknown model delivery.
            deadline=time.monotonic()+5
            while self.f.generation()!=1 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(self.f.generation(),1)
        self.stop();self.start()
        self.assertEqual(self.request('/status',ref)[1]['state'],'committed')
        self.assertEqual(self.request('/execute',{**ref,'recover':True})[1]['generation'],1)
        self.assertEqual(self.f.generation(),1)
    def test_pending_publication_failure_survives_actual_server_restart(self):
        self.stop();self.start('fail_publish');ref=self.ref();before=self.f.h.config_path.read_bytes()
        status,value,_=self.request('/execute',{**ref,'recover':False})
        self.assertEqual(status,503);self.assertEqual(value['error'],'outcome_unconfirmed')
        self.assertEqual(self.request('/status',ref)[1]['state'],'pending')
        self.assertEqual(self.f.h.config_path.read_bytes(),before);self.assertEqual(self.f.generation(),0)
        self.stop();self.start()
        self.assertEqual(self.request('/status',ref)[1]['state'],'pending');self.assertEqual(self.f.generation(),0)
        self.assertEqual(self.request('/execute',{**ref,'recover':True})[0],200)
        self.assertEqual(self.f.h.get('legacy')['history_n'],73);self.assertEqual(self.f.generation(),1)
    def test_extra_privilege_or_dispatch_cannot_enter_via_tcp(self):
        for extra in ({'access':{}},{'principal_id':'admin'},{'method':'delete'}):
            self.assertEqual(self.request('/prepare',{'request_id':'x','history_n':5,**extra})[0],400)
        self.assertEqual(self.f.generation(),0);self.assertEqual(list(self.directory.iterdir()),[])


if __name__=='__main__':unittest.main()
