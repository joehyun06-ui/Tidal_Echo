"""Candidate startup from explicit synthetic plans; no production configuration."""
import ast
import __future__
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from backend.usage_ledger import LedgerError, build_view
from backend.usage_ledger_entrypoint import load_loop, install_relay, MODE, PLAN, ACTIVATION, CONTINUATION_KEY, FINGERPRINT_KEY
from backend.usage_ledger_preflight import mock_plan, MAX_BYTES
from backend.tests.test_usage_ledger_composition import CompositionFixture, KEY, PREFIX_KEY, ROUTE, RAW, ROOT


class EntryFixture(CompositionFixture):
    def setUp(self):
        super().setUp()
        self.plan = mock_plan()
        self.plan['storage'] = {'persistent_root':str(self.root), 'ledger_db':str(self.path),
                                'relay_db':str(self.relay), 'codex_db':str(self.native)}
        self.plan['codex'] = None
        self.plan_path = self.root/'ledger-plan.json'
        self.env = {MODE:'capture', PLAN:str(self.plan_path), ACTIVATION:'11111111-1111-4111-8111-111111111111',
                    CONTINUATION_KEY:KEY.decode(), FINGERPRINT_KEY:PREFIX_KEY.decode()}

    def load(self, *, write=True, **env_changes):
        if write: self.plan_path.write_text(json.dumps(self.plan))
        return load_loop({**self.env, **env_changes}, persistent_root=self.root, relay_db=self.relay, native_db=self.native)


class EntrypointTests(EntryFixture):
    def test_disabled_reads_only_mode_and_never_constructs_services_or_proxy(self):
        class ModeOnly(dict):
            def get(self, key, default=None):
                if key != MODE: raise AssertionError('disabled must not inspect '+key)
                return default
        with patch('pathlib.Path.open', side_effect=AssertionError('no files')), \
             patch('backend.usage_ledger_entrypoint.prepare', side_effect=AssertionError('no services')):
            self.assertIsNone(load_loop(ModeOnly(), persistent_root=object(), relay_db=object(), native_db=object()))
            self.assertFalse(install_relay(object(), ModeOnly()))
        self.assertFalse(self.path.exists())

    def test_disabled_ignores_stale_plan_and_key_values_without_reading_them(self):
        self.assertIsNone(self.load(**{MODE:'disabled', PLAN:'/not/read/private.json', CONTINUATION_KEY:object()}))
        self.assertFalse(self.path.exists())

    async def test_api_capture_uses_validated_profiles_and_actual_usage_without_native_billing(self):
        before = copy.deepcopy(self.env)
        services = self.load()
        attempt = services.api.begin(ROUTE, 'chat', 'a', 'synthetic-turn'); attempt.usage(RAW); await attempt.finish(None)
        self.assertEqual(services.ledger.records('api')[0]['normalized']['metrics']['input'], 100)
        self.assertIsNone(services.codex)
        self.assertEqual(self.env, before)

    def test_native_only_capture_does_not_read_api_fingerprint_secret(self):
        self.plan['api'] = None; self.plan['codex'] = mock_plan()['codex']
        services = self.load(**{FINGERPRINT_KEY:None})
        self.assertIsNone(services.api); self.assertIsNotNone(services.codex)
        self.assertFalse(services.codex.exclusive_threads)

    def test_private_key_file_configures_native_capture_without_reading_api_key_file(self):
        import os
        if not all(hasattr(os, name) for name in ('O_NOFOLLOW','O_DIRECTORY','geteuid')):
            return  # File mode is POSIX-only; environment-based startup is unchanged.
        private=self.root/'keys';private.mkdir(mode=0o700)
        key=private/'continuation.key';key.write_bytes(KEY);key.chmod(0o600)
        self.plan['api']=None;self.plan['codex']=mock_plan()['codex']
        env={CONTINUATION_KEY:None,CONTINUATION_KEY+'_FILE':str(key),
             FINGERPRINT_KEY:object(),FINGERPRINT_KEY+'_FILE':object()}
        services=self.load(**env)
        self.assertIsNotNone(services.codex)
        self.assertIsNone(services.api)

    def test_file_failures_are_redacted_before_any_ledger_creation(self):
        for path in (str(self.root/'private-missing.key'),'/tmp/private-outside.key'):
            with self.assertRaisesRegex(LedgerError,'^ledger_entrypoint_unavailable$') as caught:
                self.load(**{CONTINUATION_KEY:None,CONTINUATION_KEY+'_FILE':path})
            self.assertNotIn('private',str(caught.exception))
            self.assertFalse(self.path.exists())

    def test_history_reads_no_fingerprint_secret_and_preserves_pause_evidence(self):
        services = self.load(**{MODE:'history', FINGERPRINT_KEY:object()})
        self.assertIsNone(services.api); self.assertIsNone(services.codex)
        self.assertEqual(build_view(services.ledger,'api','a')['collection_health']['capture_pauses'], 1)

    def test_bad_modes_fail_with_fixed_error_before_files(self):
        for mode in ('', 'true', 'CAPTURE', None, True):
            with self.assertRaisesRegex(LedgerError, '^ledger_entrypoint_invalid$'): self.load(**{MODE:mode})
        self.assertFalse(self.path.exists())

    def test_missing_short_or_header_like_secrets_never_create_ledger_or_leak(self):
        for field, value in ((CONTINUATION_KEY,None), (CONTINUATION_KEY,'synthetic-private'),
                             (FINGERPRINT_KEY,None), (FINGERPRINT_KEY,'private\n'+'k'*40)):
            with self.assertRaisesRegex(LedgerError, '^ledger_entrypoint_unavailable$') as caught:
                self.load(**{field:value})
            self.assertNotIn('private', str(caught.exception)); self.assertFalse(self.path.exists())

    def test_source_and_persistent_root_mismatch_fail_before_creation(self):
        original = copy.deepcopy(self.plan)
        for key in ('relay_db', 'codex_db', 'persistent_root'):
            self.plan = copy.deepcopy(original); self.plan['storage'][key] = str(self.root/'wrong-private-source')
            with self.assertRaisesRegex(LedgerError, '^ledger_entrypoint_unavailable$'): self.load()
            self.assertFalse(self.path.exists())

    def test_plan_must_be_json_inside_trusted_root_and_not_symlink(self):
        self.plan_path.write_text(json.dumps(self.plan))
        link = self.root/'linked-private.json'; link.symlink_to(self.plan_path)
        for path in (str(link), '/tmp/untrusted-private.json', str(self.root/'private.env'), 'relative.json'):
            with self.assertRaisesRegex(LedgerError, '^ledger_entrypoint_unavailable$'): self.load(write=False, **{PLAN:path})
            self.assertFalse(self.path.exists())

    def test_duplicate_or_oversized_plan_is_redacted(self):
        for data in ('{"schema_version":1,"schema_version":1}', 'x'*(MAX_BYTES+1)):
            self.plan_path.write_text(data)
            with self.assertRaisesRegex(LedgerError, '^ledger_entrypoint_unavailable$'): self.load(write=False)
        self.assertFalse(self.path.exists())

    def test_unreviewed_capture_or_credential_fields_cannot_activate(self):
        self.plan['hooks']['authenticated_routes'] = False
        with self.assertRaises(LedgerError): self.load()
        self.plan['hooks']['authenticated_routes'] = True
        self.plan['api']['profiles'][0]['key'] = 'synthetic-private-key'
        with self.assertRaisesRegex(LedgerError, '^ledger_entrypoint_unavailable$'): self.load()
        self.assertFalse(self.path.exists())

    def test_real_entrypoint_constructor_expansions_use_the_prepared_collectors(self):
        # Execute the actual kwargs expressions without importing the live entrypoint.
        services = self.load()
        tree = ast.parse((ROOT/'examples/api_loop_codex_canary.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in {'ApiWebRuntime','StreamingCodexGenerationRuntime'}]
        got = {}
        for call in calls:
            for kw in call.keywords:
                if kw.arg is None:
                    got[call.func.id] = eval(compile(ast.Expression(kw.value), '<candidate-ledger-kwargs>', 'eval'),
                                             {'LEDGER_SERVICES':services})
        self.assertIs(got['ApiWebRuntime']['usage_collector'], services.api)
        self.assertNotIn('usage_collector', got['StreamingCodexGenerationRuntime'])
        self.assertIs(got['StreamingCodexGenerationRuntime']['compaction_collector'], services.manual_compactions)


class EntrypointRouteTests(EntryFixture):
    def delete_endpoint(self, services):
        # Run the actual route function, with only its entrypoint globals supplied.
        from fastapi.responses import JSONResponse
        from backend import web_session_delete, web_session_provider_authority
        tree = ast.parse((ROOT/'examples/api_loop_codex_canary.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'loop_sessions_delete')
        node.decorator_list = []
        self.legacy.check_internal_auth = lambda request: None
        namespace = {'legacy':self.legacy, 'INTEGRATION':SimpleNamespace(session_authority=self.authority, _session_lock=self.lock),
                     'GENERATION_CONFIG':SimpleNamespace(store_path=self.native), '_upload_dir':lambda:None,
                     'LEDGER_SERVICES':services, 'web_session_delete':web_session_delete,
                     'web_session_provider_authority':web_session_provider_authority, 'sqlite3':sqlite3,
                     'JSONResponse':JSONResponse, '_authority_error':lambda error:(_ for _ in ()).throw(error)}
        exec(compile(ast.Module(body=[node],type_ignores=[]), '<actual-session-delete>', 'exec',
                     flags=__future__.annotations.compiler_flag), namespace)
        return namespace['loop_sessions_delete']

    async def test_actual_ordinary_delete_route_retires_ledger_and_blocks_late_write(self):
        writer = self.bundle(capture=True)
        late = writer.api.begin(ROUTE,'chat','a','late-turn')
        services = self.load(**{MODE:'history'}); services.bind_authority(self.authority,self.lock)
        result = await self.delete_endpoint(services)('a',object())
        self.assertTrue(result['deleted']['ledger_deleted'])
        late.usage(RAW); await late.finish(None)
        self.assertEqual(writer.ledger.records('api','a'),[])

    async def test_actual_delete_storage_failure_is_unconfirmed_redacted_and_retryable(self):
        services = self.load(**{MODE:'history'}); services.bind_authority(self.authority,self.lock)
        endpoint = self.delete_endpoint(services)
        with patch.object(services.ledger,'purge_session',side_effect=OSError('synthetic-private-storage')):
            result = await endpoint('a',object())
        self.assertEqual(result.status_code,503)
        self.assertTrue(json.loads(result.body)['deletion_unconfirmed'])
        self.assertNotIn('synthetic-private',result.body.decode())
        self.assertIsNone(self.authority.row_for_session('a'))
        retried = await endpoint('a',object())
        self.assertTrue(retried['deleted']['ledger_deleted'])

    async def test_disabled_delete_preserves_previous_exception_behavior(self):
        with patch('backend.web_session_delete.delete_conversation',side_effect=OSError('synthetic-only')):
            with self.assertRaises(OSError):await self.delete_endpoint(None)('a',object())

    async def test_actual_version_installer_expansion_purges_every_version(self):
        import httpx
        from fastapi import FastAPI
        from backend import web_message_versions, web_session_fork_routes
        from backend.codex_generation_protocol import CodexProcessActivityGate
        services = self.load(**{MODE:'history'}); services.bind_authority(self.authority,self.lock)
        self.authority.publish_row(self.authority.new_row(provider='api',session_id='a-alt'),activate=False)
        with web_message_versions.connect(self.relay) as c:
            c.execute("INSERT INTO web_message_versions VALUES ('a-alt','a','a','regenerate',1,1,1)")
        self.legacy.check_internal_auth = lambda request:None
        app = FastAPI(); runtime = SimpleNamespace(config=SimpleNamespace(store_path=self.native),
            foundation=SimpleNamespace(activity_gate=CodexProcessActivityGate()))
        namespace = {'app':app,'legacy':self.legacy,'RUNTIME':runtime,'LEDGER_SERVICES':services,
                     'INTEGRATION':SimpleNamespace(session_authority=self.authority,_session_lock=self.lock),
                     'web_session_fork_routes':web_session_fork_routes}
        tree = ast.parse((ROOT/'examples/api_loop_codex_canary.py').read_text())
        node = next(n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                    and isinstance(n.value.func,ast.Attribute) and isinstance(n.value.func.value,ast.Name)
                    and n.value.func.value.id=='web_session_fork_routes')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-version-installer>','exec'),namespace)
        writer = self.bundle(capture=True)
        late = [writer.api.begin(ROUTE,'chat',sid,'version-turn') for sid in ('a','a-alt')]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline') as client:
            result = await client.delete('/loop/conversations/a')
        self.assertEqual(result.status_code,200)
        for attempt in late:attempt.usage(RAW);await attempt.finish(None)
        self.assertEqual(writer.ledger.records('api'),[])
        self.assertIsNone(self.authority.row_for_session('a-alt'))


class EntrypointImportTests(EntryFixture):
    def environment(self, root, mode):
        # Reuse the existing isolated entrypoint fixture; never import the live app here.
        from backend.tests import test_codex_canary_entrypoint_imports as fixture
        runtime_env = {k:os.environ[k] for k in ('PATH','LANG','LC_ALL','LD_LIBRARY_PATH') if k in os.environ}
        # Do not inherit parent tests' feature switches, persona paths or secrets.
        # Patch this fixture module's os binding, never the process environment.
        with patch.object(fixture,'os',SimpleNamespace(environ=runtime_env)):
            env = fixture.CodexCanaryEntrypointImportTest()._base_env(root)
        persistent = root/'persistent'; persistent.mkdir(exist_ok=True)
        plan = mock_plan(); plan['codex'] = None
        plan['storage'] = {'persistent_root':str(persistent),'ledger_db':str(persistent/'ledger.sqlite'),
                           'relay_db':env['RELAY_DB'],'codex_db':env['CODEX_GENERATION_DB']}
        plan_path = persistent/'ledger-plan.json';plan_path.write_text(json.dumps(plan))
        env.update({MODE:mode,PLAN:str(plan_path),ACTIVATION:'11111111-1111-4111-8111-111111111111',
                    CONTINUATION_KEY:KEY.decode(),FINGERPRINT_KEY:PREFIX_KEY.decode(),
                    'LEGACY_CHAT_BRIDGE_TOKEN':'synthetic-bridge-token-only-1234567890',
                    'RELAY_LOOP_INGEST_URL':'http://127.0.0.1:3020/loop/ingest'})
        return env

    def test_environment_isolated_from_parent_feature_and_persona_flags(self):
        parent = {'PATH':'/synthetic-path','MEMORY_EXPLICIT_WRITES_ENABLED':'true',
                  'PERSONA_FILE':'synthetic-private-persona','GUITING_LEDGER_CONTINUATION_KEY':'private-parent-key'}
        with patch.object(sys.modules[__name__],'os',SimpleNamespace(environ=parent)):
            env = self.environment(self.root,'disabled')
        self.assertEqual(env['PATH'],'/synthetic-path')
        self.assertEqual(env['MEMORY_CORE_ENABLED'],'false')
        self.assertNotIn('MEMORY_EXPLICIT_WRITES_ENABLED',env); self.assertNotIn('PERSONA_FILE',env)
        self.assertEqual(env[CONTINUATION_KEY],KEY.decode())

    def run_script(self, script, mode):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=self.environment(Path(tmp),mode),
                                    capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('ENTRYPOINT_OK',result.stdout)

    def test_real_loop_entrypoint_import_in_disabled_history_and_api_capture_modes(self):
        script = '''
import socket,os
def forbidden(*a,**k):raise AssertionError('offline-only')
socket.socket.connect=forbidden;socket.getaddrinfo=forbidden
import examples.api_loop_codex_canary as m
s=m.LEDGER_SERVICES;mode=os.environ['GUITING_LEDGER_MODE']
paths=[getattr(r,'path','') for r in m.app.routes]
if mode=='disabled':
 assert s is None
 assert not any('usage-ledger' in p for p in paths)
else:
 assert s is not None and m.API_GENERATIONS.usage_collector is s.api
 assert m.RUNTIME.usage_collector is None
 assert m.RUNTIME.compactions.usage_collector is s.manual_compactions
 assert m.INTEGRATION.api_generations is m.API_GENERATIONS
 assert len([p for p in paths if 'usage-ledger' in p])==4
 assert s.ledger.records('api')==[]
 assert (s.api is not None)==(mode=='capture')
assert m.RUNTIME.generation_enabled is False
print('ENTRYPOINT_OK')
'''
        for mode in ('disabled','history','capture'):self.run_script(script,mode)

    def test_real_p3_relay_entrypoint_mounts_proxy_only_when_selected(self):
        script = '''
import socket,os
def forbidden(*a,**k):raise AssertionError('offline-only')
socket.socket.connect=forbidden;socket.getaddrinfo=forbidden
import backend.p3_codex_relay_app as m
paths=[getattr(r,'path','') for r in m.bridge.relay_app.app.routes]
assert len([p for p in paths if 'usage-ledger' in p])==(0 if os.environ['GUITING_LEDGER_MODE']=='disabled' else 4)
assert m.app is m.bridge.app
print('ENTRYPOINT_OK')
'''
        for mode in ('disabled','history','capture'):self.run_script(script,mode)
