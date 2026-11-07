"""Full app TCP: explicit strict-read mode and cooperative history snapshot."""
import http.client
import json
import os
import unittest
from backend.tests import test_session_lifecycle_history_tcp as tcp


@unittest.skipUnless(os.name=='posix','private POSIX loopback reads')
class ReadInterlockTcpTests(unittest.TestCase):
    def setUp(self):
        self.f=tcp.HistoryTcpTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.f.stop();self.f.start('strict')
    def get(self,path,token=tcp.TOKEN):
        c=http.client.HTTPConnection('127.0.0.1',self.f.port,timeout=5)
        try:
            headers={} if token is None else {'x-api-loop-internal-token':token}
            c.request('GET',path,headers=headers);r=c.getresponse();return r.status,json.loads(r.read())
        finally:c.close()
    def test_actual_old_reads_blocked_and_coordinated_projection_tracks_commit(self):
        before=self.f.f.h.config_path.read_bytes()
        for path in ('/loop/config','/loop/sessions','/loop/provider/status','/healthz'):
            status,value=self.get(path);self.assertEqual(status,423);self.assertEqual(value['error'],'lifecycle_reader_blocked')
        self.assertEqual(before,self.f.f.h.config_path.read_bytes());self.assertEqual(self.f.f.generation(),0)
        self.assertEqual(self.get(tcp.PREFIX,None)[0],401)
        self.assertEqual(self.get(tcp.PREFIX)[1],{'ok':True,'history_n':24,'generation':0,'observed':True})
        ref=self.f.ref();self.assertEqual(self.f.request('/execute',{**ref,'recover':False})[0],200)
        self.assertEqual(self.get(tcp.PREFIX)[1]['history_n'],73);self.assertEqual(self.get(tcp.PREFIX)[1]['generation'],1)
    def test_default_non_strict_mode_retains_old_read_behavior(self):
        self.f.stop();self.f.start('interlocked')
        self.assertEqual(self.get('/loop/config')[0],200)
        self.assertEqual(self.get(tcp.PREFIX)[0],200)


if __name__=='__main__':unittest.main()
