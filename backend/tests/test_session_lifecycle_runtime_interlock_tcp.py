"""Full app runtime interlock without the HTTP mutation-method interlock."""
import http.client
import json
import os
import unittest
from backend.tests import test_session_lifecycle_history_tcp as tcp


@unittest.skipUnless(os.name=='posix','private POSIX runtime admission')
class RuntimeInterlockTcpTests(unittest.TestCase):
    def test_actual_ingest_and_provider_functions_refuse_but_metadata_remains_available(self):
        f=tcp.HistoryTcpTests();self.addCleanup(f.doCleanups);f.setUp()
        f.stop();f.start('runtime_locked')
        self.assertTrue(f.ready['runtime_entrypoints_guarded'])
        before=f.f.h.config_path.read_bytes();messages=f.f.h.conn.execute('SELECT * FROM messages').fetchall()
        c=http.client.HTTPConnection('127.0.0.1',f.port,timeout=5)
        try:
            c.request('POST','/loop/ingest',json.dumps({'text':'synthetic','session_id':'synthetic','dry':True}),
                {'x-api-loop-internal-token':tcp.TOKEN,'Content-Type':'application/json'})
            r=c.getresponse();body=json.loads(r.read());self.assertEqual(r.status,423)
            self.assertEqual(body['detail'],'lifecycle_runtime_unavailable')
        finally:c.close()
        self.assertEqual(before,f.f.h.config_path.read_bytes());self.assertEqual(f.f.generation(),0)
        self.assertEqual(messages,f.f.h.conn.execute('SELECT * FROM messages').fetchall())
        ref=f.ref();self.assertEqual(f.request('/execute',{**ref,'recover':False})[0],200)
        self.assertEqual(f.request('/status',ref)[1]['state'],'committed');self.assertEqual(f.f.generation(),1)


if __name__=='__main__':unittest.main()
