"""Selected real API-loop function globals rebound before startup."""
import ast
import json
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import patch
from fastapi import FastAPI,HTTPException
from backend import deployment_config
from backend.session_lifecycle_loop_config_io import install_api_loop_config_io,NAMES
from backend.tests import test_session_lifecycle_model_commands as fixtures
from backend.tests.test_session_lifecycle_history_packet import binding
from backend.tests.test_session_lifecycle_commands import commands
from backend.tests.test_session_lifecycle import CID


def module(c):
    path=Path(__file__).resolve().parents[2]/'examples'/'api_loop.py'
    nodes=[n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in NAMES]
    code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    m=ModuleType('synthetic_config_loop')
    m.__dict__.update(LOOP_CONFIG=c.config,RELAY_DB=c.canonical_path,app=FastAPI(),RENDER_TELEGRAM_MVP=True,
        HISTORY_N=88,json=json,HTTPException=HTTPException,deployment_config=deployment_config)
    exec(compile(ast.fix_missing_locations(code),str(path),'exec'),m.__dict__);return m


class LoopConfigIoTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        directory=self.f.root/'packets';directory.mkdir(mode=0o700)
        self.a=binding(self.f.h.path,directory);self.loop=module(self.a.commands.coordinator)
    def install(self):return install_api_loop_config_io(self.loop,self.a,access=fixtures.ACCESS,enabled=True)
    def error(self,fn,status):
        with self.assertRaises(HTTPException) as caught:fn()
        self.assertEqual(caught.exception.status_code,status)
    def test_default_off_does_not_access_dependencies_or_replace_globals(self):
        old=self.loop.load_config;self.assertFalse(install_api_loop_config_io(None,None,access=None))
        self.assertIs(self.loop.load_config,old)
    def test_current_reads_use_coordinator_and_return_independent_private_copy(self):
        self.install();cfg=self.loop.load_config();cfg['main_chain'][0]['model']='local edit'
        self.assertEqual(self.loop.load_config()['main_chain'][0]['model'],fixtures.OLD)
        self.assertEqual(self.loop.history_n(),24);self.assertEqual(self.f.generation(),0)
    def test_old_writes_refuse_before_original_functions_or_id_allocation(self):
        self.install();before=self.f.h.config_path.read_bytes()
        for call in (lambda:self.loop.save_config({}),lambda:self.loop.update_config({'history_n':3}),
                     lambda:self.loop.patch_session(CID,{'pinned':True}),lambda:self.loop.create_session(),lambda:self.loop.save_sessions([])):
            self.error(call,423)
        self.assertEqual(before,self.f.h.config_path.read_bytes());self.assertEqual(self.f.generation(),0)
    def test_only_same_command_instance_can_use_supported_coordinated_branch(self):
        self.install()
        result=self.loop.update_config({'history_n':73},coordinated=self.a.commands,request_id='same',access=fixtures.ACCESS)
        self.assertEqual(result.generation,1);self.assertEqual(self.loop.history_n(),73)
        other=commands(self.a.commands.coordinator)
        self.error(lambda:self.loop.update_config({'history_n':3},coordinated=other,request_id='other',access=fixtures.ACCESS),423)
        self.assertEqual(self.f.generation(),1)
    def test_pending_read_failure_is_not_swallowed_by_original_history_default(self):
        self.install()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            with self.assertRaises(fixtures.LifecycleError):
                self.a.commands.update_config('pending',{'history_n':73},access=fixtures.ACCESS)
        before=self.f.h.config_path.read_bytes();self.error(self.loop.load_config,409);self.error(self.loop.history_n,409)
        self.assertEqual(before,self.f.h.config_path.read_bytes());self.assertEqual(self.f.generation(),0)
    def test_current_access_loss_blocks_direct_reads(self):
        self.install();self.a.commands._authorize=lambda *_:False
        self.error(self.loop.load_config,403);self.error(self.loop.history_n,403)
    def test_wrong_resource_rejected_without_partial_rebinding(self):
        original=self.loop.load_config;self.loop.LOOP_CONFIG=self.f.root/'other.json'
        with self.assertRaises(ValueError):self.install()
        self.assertIs(self.loop.load_config,original);self.assertFalse(getattr(self.loop,'session_lifecycle_config_io_bound',False))
    def test_duplicate_or_already_wrapped_module_rejected(self):
        self.loop.save_config=lambda *_:None
        with self.assertRaisesRegex(ValueError,'unwrapped'):self.install()
        self.loop=module(self.a.commands.coordinator);self.install()
        with self.assertRaises(ValueError):self.install()
    def test_changed_module_resource_is_not_silently_read_from_old_binding(self):
        self.install();self.loop.LOOP_CONFIG=self.f.root/'retargeted.json'
        self.error(self.loop.load_config,503);self.error(self.loop.history_n,503)
        self.error(lambda:self.loop.update_config({'history_n':7},coordinated=self.a.commands,request_id='changed',access=fixtures.ACCESS),503)
        self.assertFalse(self.loop.LOOP_CONFIG.exists());self.assertEqual(self.f.generation(),0)


if __name__=='__main__':unittest.main()
