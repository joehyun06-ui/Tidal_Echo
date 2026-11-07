"""Complete module captures original aliases before explicit IO binding."""
import os
import unittest
from backend.tests import test_session_lifecycle_history_tcp as tcp


@unittest.skipUnless(os.name=='posix','private POSIX aliases')
class ConfigAliasTcpTests(unittest.TestCase):
    def test_complete_app_original_alias_probe_and_original_request_still_work(self):
        f=tcp.HistoryTcpTests();self.addCleanup(f.doCleanups);f.setUp()
        f.stop();f.start('aliases_bound')
        self.assertTrue(f.ready['captured_aliases_guarded']);self.assertEqual(f.f.generation(),0)
        ref=f.ref();self.assertEqual(f.request('/execute',{**ref,'recover':False})[0],200)
        self.assertEqual(f.f.generation(),1);self.assertEqual(f.f.h.get('legacy')['history_n'],73)


if __name__=='__main__':unittest.main()
