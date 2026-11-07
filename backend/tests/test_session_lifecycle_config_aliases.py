"""Captured references to current original source obey the installed binding."""
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from backend.tests import test_session_lifecycle_loop_config_io as io
from backend.tests.test_session_lifecycle import CID


class ConfigAliasTests(unittest.TestCase):
    def setUp(self):
        self.f=io.LoopConfigIoTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.aliases={name:getattr(self.f.loop,name) for name in io.NAMES}
    def blocked(self,fn,status):self.f.error(fn,status)
    def test_captured_original_writes_refuse_before_original_persistence(self):
        self.f.install();before=self.f.f.h.config_path.read_bytes()
        for fn in (lambda:self.aliases['save_config']({}),lambda:self.aliases['update_config']({'history_n':3}),
                   lambda:self.aliases['patch_session'](CID,{'pinned':True}),lambda:self.aliases['create_session'](),
                   lambda:self.aliases['save_sessions']([])):
            self.blocked(fn,423)
        self.assertEqual(before,self.f.f.h.config_path.read_bytes());self.assertEqual(self.f.f.generation(),0)
    def test_captured_reads_use_current_grant_and_do_not_swallow_pending(self):
        self.f.install();self.assertEqual(self.aliases['history_n'](),24)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            with self.assertRaises(io.fixtures.LifecycleError):
                self.f.a.commands.update_config('pending',{'history_n':73},access=io.fixtures.ACCESS)
        self.blocked(self.aliases['load_config'],409);self.blocked(self.aliases['history_n'],409)
        self.f.a.commands._authorize=lambda *_:False
        self.blocked(self.aliases['load_config'],403);self.blocked(self.aliases['history_n'],403)
    def test_captured_supported_coordinated_branch_executes_once_without_recursion(self):
        self.f.install();r=self.aliases['update_config']({'history_n':73},coordinated=self.f.a.commands,
            request_id='alias',access=io.fixtures.ACCESS)
        self.assertEqual(r.generation,1);self.assertEqual(self.aliases['history_n'](),73)
        r=self.aliases['update_config']({'history_n':73},coordinated=self.f.a.commands,request_id='alias',access=io.fixtures.ACCESS)
        self.assertTrue(r.replayed);self.assertEqual(self.f.f.generation(),1)
    def test_uninstalled_current_source_alias_retains_original_behavior(self):
        before=self.f.loop.load_config()
        self.aliases['save_config'](before)
        self.assertEqual(self.aliases['history_n'](),24)
        self.assertFalse(hasattr(self.f.loop,'_session_lifecycle_config_io'))


if __name__=='__main__':unittest.main()
