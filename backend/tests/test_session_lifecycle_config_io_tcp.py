"""Full app direct-global binding, without relying on HTTP interlock."""
import http.client
import json
import os
import unittest
from backend.tests import test_session_lifecycle_history_tcp as tcp


@unittest.skipUnless(os.name=='posix','private POSIX config IO')
class ConfigIoTcpTests(unittest.TestCase):
    def setUp(self):
        self.f=tcp.HistoryTcpTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.f.stop();self.f.start('direct_bound')
    def request(self,method,path,body=None):
        c=http.client.HTTPConnection('127.0.0.1',self.f.port,timeout=5)
        try:
            c.request(method,path,None if body is None else json.dumps(body),{'x-api-loop-internal-token':tcp.TOKEN,'Content-Type':'application/json'})
            r=c.getresponse();return r.status,json.loads(r.read())
        finally:c.close()
    def test_real_legacy_handlers_reach_bound_functions_and_cannot_write(self):
        before=self.f.f.h.config_path.read_bytes()
        for method,path,body in (('POST','/loop/config',{'history_n':2}),('POST','/loop/sessions',{'title':'blocked'}),
                                 ('PATCH','/loop/sessions/synthetic',{'pinned':True})):
            status,value=self.request(method,path,body);self.assertEqual(status,423);self.assertEqual(value['detail'],'lifecycle_direct_write_blocked')
        self.assertEqual(before,self.f.f.h.config_path.read_bytes());self.assertEqual(self.f.f.generation(),0)
        self.assertEqual(self.request('GET','/loop/config')[0],200)
        ref=self.f.ref();self.assertEqual(self.f.request('/execute',{**ref,'recover':False})[0],200)
        self.assertEqual(self.f.f.h.get('legacy')['history_n'],73)
        self.assertEqual(self.request('GET','/loop/config')[0],200)
    def test_pending_blocks_old_read_handlers_until_explicit_original_recovery(self):
        self.f.stop();self.f.start('direct_bound_fail_publish');ref=self.f.ref()
        self.assertEqual(self.f.request('/execute',{**ref,'recover':False})[0],503)
        self.assertEqual(self.request('GET','/loop/config')[0],409)
        self.assertEqual(self.request('GET','/loop/sessions')[0],409)
        self.assertEqual(self.f.request('/status',ref)[1]['state'],'pending')
        self.f.stop();self.f.start('direct_bound')
        self.assertEqual(self.request('GET','/loop/config')[0],409)
        self.assertEqual(self.f.request('/execute',{**ref,'recover':True})[0],200)
        self.assertEqual(self.request('GET','/loop/config')[0],200);self.assertEqual(self.f.f.generation(),1)


if __name__=='__main__':unittest.main()
