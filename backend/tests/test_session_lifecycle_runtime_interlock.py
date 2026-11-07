"""Original async aliases refuse before provider/body/sink/callback effects."""
import ast
import functools
from pathlib import Path
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock
from fastapi import FastAPI,HTTPException
from backend.session_lifecycle_runtime_interlock import install_api_loop_runtime_interlock,NAMES
from backend import usage_ledger_capture


def module():
    path=Path(__file__).resolve().parents[2]/'examples'/'api_loop.py'
    nodes=[n for n in ast.parse(path.read_text()).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in (*NAMES,'_lifecycle_runtime_guarded')]
    m=ModuleType('synthetic_runtime_loop');m.app=FastAPI();m.session_lifecycle_config_io_bound=True
    m.httpx=SimpleNamespace(AsyncClient=Mock(return_value=object()))
    m.functools=functools;m.usage_ledger_capture=usage_ledger_capture
    code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(code),str(path),'exec'),m.__dict__);return m


class RuntimeInterlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.m=module();self.aliases={n:getattr(self.m,n) for n in NAMES}
    async def test_all_original_async_aliases_refuse_before_route_or_network_access(self):
        install_api_loop_runtime_interlock(self.m,enabled=True)
        sink=Mock(side_effect=AssertionError('sink'))
        calls=[('relay_out',({},),{}),('stream_responses',({},[],sink),{}),('complete_responses',({},[]),{}),
            ('stream_chat',({},[],sink),{}),('complete_chat',({},[]),{}),('_stream_provider',({},[],sink),{}),
            ('_complete_provider',({},[]),{}),('run_model',([],),{}),
            ('run_kelivo_provider_contract',('synthetic',[]),{'temperature':0.1,'max_tokens':1}),
            ('handle_ingest',('synthetic',None,'synthetic'),{})]
        for name,args,kwargs in calls:
            with self.assertRaises(HTTPException) as e:await self.aliases[name](*args,**kwargs)
            self.assertEqual(e.exception.status_code,423,name)
        sink.assert_not_called();self.m.httpx.AsyncClient.assert_not_called()
    async def test_factory_alias_refuses_before_client_construction(self):
        install_api_loop_runtime_interlock(self.m,enabled=True)
        with self.assertRaises(HTTPException):self.aliases['_provider_client']()
        self.m.httpx.AsyncClient.assert_not_called()
    async def test_default_off_keeps_original_factory_behavior_without_request(self):
        self.assertFalse(install_api_loop_runtime_interlock(None))
        result=self.aliases['_provider_client'](timeout=1)
        self.assertIs(result,self.m.httpx.AsyncClient.return_value);self.m.httpx.AsyncClient.assert_called_once_with(timeout=1)
    async def test_created_but_not_started_coroutine_checks_guard_when_awaited(self):
        pending=self.aliases['run_model']([])
        install_api_loop_runtime_interlock(self.m,enabled=True)
        with self.assertRaises(HTTPException) as e:await pending
        self.assertEqual(e.exception.status_code,423)
    async def test_install_requires_prestartup_binding_and_unwrapped_functions(self):
        self.m.session_lifecycle_config_io_bound=False
        with self.assertRaises(ValueError):install_api_loop_runtime_interlock(self.m,enabled=True)
        self.m.session_lifecycle_config_io_bound=True;self.m.run_model=lambda *_:None
        with self.assertRaisesRegex(ValueError,'unwrapped'):install_api_loop_runtime_interlock(self.m,enabled=True)
        self.m=module();install_api_loop_runtime_interlock(self.m,enabled=True)
        with self.assertRaises(ValueError):install_api_loop_runtime_interlock(self.m,enabled=True)


if __name__=='__main__':unittest.main()
