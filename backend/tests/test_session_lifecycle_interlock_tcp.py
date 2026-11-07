"""Full app TCP proof: old mutation routes cannot bypass the opted-in gate."""
import http.client
import json
import os
import unittest
from backend.tests import test_session_lifecycle_history_tcp as tcp


@unittest.skipUnless(os.name=='posix','private POSIX loopback interlock')
class InterlockTcpTests(unittest.TestCase):
    def setUp(self):
        self.f=tcp.HistoryTcpTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.f.stop();self.f.start('interlocked')
    def request(self,method,path,body,token=tcp.TOKEN):
        c=http.client.HTTPConnection('127.0.0.1',self.f.port,timeout=5)
        try:
            headers={'Content-Type':'application/json'}
            if token is not None:headers['x-api-loop-internal-token']=token
            c.request(method,path,json.dumps(body),headers);r=c.getresponse()
            return r.status,json.loads(r.read())
        finally:c.close()
    def test_old_http_mutations_and_ingress_refuse_without_canonical_changes(self):
        h=self.f.f.h;before=h.config_path.read_bytes()
        messages=h.conn.execute('SELECT * FROM messages').fetchall()
        for method,path,body in (
            ('POST','/loop/config',{'history_n':99}),
            ('POST','/loop/sessions',{'title':'must not create'}),
            ('PATCH','/loop/sessions/synthetic',{'pinned':True}),
            ('POST','/loop/chat',{}),('POST','/loop/ingest',{}),
            ('POST','/loop/provider/login/start',{})):
            status,value=self.request(method,path,body)
            self.assertEqual(status,423,(path,value));self.assertEqual(value['error'],'lifecycle_writer_blocked')
        self.assertEqual(before,h.config_path.read_bytes());self.assertEqual(self.f.f.generation(),0)
        self.assertEqual(messages,h.conn.execute('SELECT * FROM messages').fetchall())
        self.assertEqual(h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)
        ref=self.f.ref();self.assertEqual(self.f.request('/execute',{**ref,'recover':False})[0],200)
        self.assertEqual(self.f.request('/status',ref)[1]['state'],'committed')
    def test_default_absent_gate_retains_existing_legacy_behavior(self):
        self.f.stop();self.f.start('normal')
        status,value=self.request('POST','/loop/config',{'history_n':33})
        self.assertEqual(status,200,value);self.assertEqual(self.f.f.h.get('legacy')['history_n'],33)
        self.assertEqual(self.f.f.generation(),0)
    def test_unauthenticated_legacy_ingress_is_denied_before_effect(self):
        before=self.f.f.h.config_path.read_bytes()
        self.assertEqual(self.request('POST','/loop/config',{'history_n':9},None)[0],401)
        self.assertEqual(before,self.f.f.h.config_path.read_bytes());self.assertEqual(self.f.f.generation(),0)


if __name__=='__main__':unittest.main()
