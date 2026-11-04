"""Offline real writer-entry bindings; AST loads pure API functions, no app boot."""
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import deployment_config, loop_config_mutations
from backend.loop_config_mutations import LoopConfigMutationError, updated_config
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_commands import ConfigCommandAccess, CoordinatedConfigCommands
from backend.web_session_provider_authority import WebSessionProviderAuthority, WebSessionProviderAuthorityError
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegration
from backend.tests.test_codex_canary_loop_integration import FakeLegacy, FakeController, FakeControl
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator

ACCESS = ConfigCommandAccess('synthetic_owner')
ROUTES = [{'model':'synthetic','url':'https://synthetic.invalid/v1','key':'synthetic secret'}]


def commands(c, *, lock=None, authorize=lambda *_: True, enabled=True):
    return CoordinatedConfigCommands(c, session_lock=lock or RLock(), authorize=authorize,
        historical_provider=lambda _: None, fallback_routes=lambda:deepcopy(ROUTES), enabled=enabled)


def api_entries():
    """Execute entire existing entry functions without FastAPI/startup/network.

    Full-module imports/HTTP behavior stay covered by the existing Python CI.
    No extracted function implementation is duplicated in the test.
    """
    path = Path(__file__).resolve().parents[2]/'examples'/'api_loop.py'
    source = ast.parse(path.read_text())
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in {'patch_session','update_config'}]
    class HTTPError(Exception):
        def __init__(self, *, status_code, detail): self.status_code,self.detail=status_code,detail
    namespace = {'__name__':'synthetic_api_entries', 'Any':object, 'deployment_config':deployment_config,
                 'loop_config_mutations':loop_config_mutations, 'HTTPException':HTTPError,
                 'RENDER_TELEGRAM_MVP':True, 'env_routes':lambda:deepcopy(ROUTES)}
    module=ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    code = compile(ast.fix_missing_locations(module), str(path), 'exec')
    exec(code, namespace)
    return namespace


def process_writer(path, mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        c = coordinator(path)
        writer = commands(c)
        if mode == 'settings':
            calls = []
            def authorize(*_):
                calls.append(1)
                if len(calls) == 3:
                    print('ready', flush=True)
                    sys.stdin.readline()
                return True
            writer._authorize = authorize
            api_entries()['update_config']({'history_n':77}, coordinated=writer, request_id='settings', access=ACCESS)
        elif mode == 'crash':
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_: os._exit(72)):
                writer.patch_session('patch',CID,{'pinned':True},access=ACCESS)
        else:
            api_entries()['patch_session'](CID, {'title':'process title'}, coordinated=writer, request_id='title', access=ACCESS)


class PureConfigMutationTests(unittest.TestCase):
    def test_masked_key_and_url_are_kept_while_model_and_history_change(self):
        config = {'main_chain':deepcopy(ROUTES),'sessions':[{'id':'synthetic'}]}
        body = {'main_chain':[{'index':0,'model':'new model','url':'','key':''}],'history_n':999}
        original, request = deepcopy(config),deepcopy(body)
        result = updated_config(config,body,render_mvp=False,fallback_routes=[])
        self.assertEqual(result['main_chain'], [{**ROUTES[0],'model':'new model'}])
        self.assertEqual(result['history_n'],200)
        self.assertEqual(config,original);self.assertEqual(body,request)
        result['sessions'][0]['id']='changed';self.assertEqual(config,original)

    def test_environment_fallback_is_only_used_when_no_materialized_route(self):
        body={'main_chain':[{'model':'new','url':'','key':''}]}
        result=updated_config({},body,render_mvp=False,fallback_routes=ROUTES)
        self.assertEqual(result['main_chain'][0]['key'],'synthetic secret')
        alternate=[{**ROUTES[0],'key':'other synthetic'}]
        result=updated_config({'main_chain':alternate},body,render_mvp=False,fallback_routes=ROUTES)
        self.assertEqual(result['main_chain'][0]['key'],'other synthetic')

    def test_mvp_invalid_payloads_keep_existing_validation_categories(self):
        for body in ({'history_n':True},{'main_chain':[]},{'main_chain':'invalid'},
                     {'main_chain':[{'model':'new','url':'','key':'','unknown':True}]}):
            with self.subTest(body=body),self.assertRaises(LoopConfigMutationError):
                updated_config({},body,render_mvp=True,fallback_routes=ROUTES)

    def test_non_mvp_history_and_multiple_routes_remain_supported(self):
        result=updated_config({}, {'history_n':'7','main_chain':[{}, {'model':'m','url':'https://synthetic.invalid/','key':'synthetic'}]},
                              render_mvp=False,fallback_routes=ROUTES)
        self.assertEqual(result['history_n'],7)
        self.assertEqual(len(result['main_chain']),2)


@unittest.skipUnless(os.name=='posix','shared gate requires POSIX')
class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        (self.root/'gate').mkdir(mode=0o700)
        config=self.h.get('legacy');config['main_chain']=deepcopy(ROUTES);self.h.put('legacy',config)
        self.c=coordinator(self.h.path);self.c.bootstrap();self.writer=commands(self.c)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught: fn()
        self.assertEqual(caught.exception.category,category)
        self.assertEqual(str(caught.exception),category)

    def patch(self,body=None,*,rid='patch',writer=None,access=ACCESS,recover=False):
        return (writer or self.writer).patch_session(rid,CID,body or {'pinned':True},access=access,recover=recover)

    def pending(self):
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.rejects(self.patch,'coordination_unavailable')

    def test_default_disabled_does_not_authorize_or_touch_dependencies(self):
        def forbidden(*_):self.fail('dependency used')
        writer=CoordinatedConfigCommands(None,session_lock=None,authorize=forbidden,
            historical_provider=forbidden,fallback_routes=forbidden)
        self.rejects(lambda:self.patch(writer=writer),'lifecycle_disabled')

    def test_denied_access_precedes_route_config_and_database_reads(self):
        writer=commands(None,authorize=lambda *_:False)
        writer._routes=lambda:self.fail('route read')
        self.rejects(lambda:writer.update_config('id',{},access=ACCESS),'coordination_access_denied')
        self.rejects(lambda:self.patch(access=None),'coordination_access_denied')

    def test_authorization_error_and_truthy_nonboolean_are_not_grants(self):
        def broken(*_):raise RuntimeError('synthetic secret /private/path')
        self.rejects(lambda:self.patch(writer=commands(self.c,authorize=broken)),'coordination_access_unavailable')
        self.rejects(lambda:self.patch(writer=commands(self.c,authorize=lambda *_:1)),'coordination_access_denied')

    def test_actual_api_patch_entry_uses_coordinator_without_legacy_io(self):
        api=api_entries()
        result=api['patch_session'](CID,{'pinned':True},coordinated=self.writer,request_id='patch',access=ACCESS)
        self.assertEqual(result.generation,1);self.assertTrue(self.h.get('legacy')['sessions'][0]['pinned'])

    def test_actual_api_config_entry_and_default_path_share_pure_implementation(self):
        api=api_entries();state=deepcopy(self.h.get('legacy'))
        api['load_config']=lambda:deepcopy(state)
        def save(config):state.clear();state.update(deepcopy(config))
        api['save_config']=save
        api['public_config']=lambda:{'synthetic_public':True}
        self.assertEqual(api['update_config']({'history_n':31}),{'synthetic_public':True})
        self.assertEqual(state['history_n'],31)
        result=api['update_config']({'history_n':42},coordinated=self.writer,request_id='settings',access=ACCESS)
        self.assertEqual(result.generation,1);self.assertEqual(self.h.get('legacy')['history_n'],42)

    def test_default_api_validation_precedes_config_io(self):
        api=api_entries();api['load_config']=lambda:self.fail('config read before validation')
        with self.assertRaises(api['HTTPException']) as caught:api['update_config']({'history_n':True})
        self.assertEqual(caught.exception.status_code,400)

    def test_actual_authority_entry_delegates_and_preserves_provider(self):
        authority=WebSessionProviderAuthority(self.h)
        receipt=authority.patch_session(CID,{'title':'new title'},coordinated=self.writer,request_id='title',access=ACCESS)
        self.assertEqual(receipt.generation,1)
        self.assertEqual(self.h.get('legacy')['sessions'][0]['provider'],'api')
        with self.assertRaises(WebSessionProviderAuthorityError):self.patch({'provider':'codex'},rid='invalid')

    def test_actual_canary_wrapper_requires_exact_existing_session_lock(self):
        legacy=FakeLegacy()
        runtime=SimpleNamespace(controller=FakeController(),control=FakeControl(),generation_enabled=False)
        integration=CodexCanaryLoopIntegration(legacy,runtime);integration.install_legacy_globals()
        writer=commands(self.c,lock=integration._session_lock)
        receipt=legacy.patch_session(CID,{'pinned':True},coordinated=writer,request_id='patch',access=ACCESS)
        self.assertEqual(receipt.generation,1)
        self.rejects(lambda:legacy.patch_session(CID,{},coordinated=self.writer,request_id='bad',access=ACCESS),'coordination_lock_mismatch')

    def test_same_id_replays_but_target_body_principal_and_action_are_bound(self):
        self.patch();self.assertTrue(self.patch().replayed)
        self.rejects(lambda:self.patch({'pinned':False}),'coordination_request_conflict')
        self.rejects(lambda:self.patch(access=ConfigCommandAccess('other_owner')),'coordination_request_conflict')
        self.rejects(lambda:self.writer.update_config('patch',{},access=ACCESS),'coordination_request_conflict')
        self.rejects(lambda:self.writer.patch_session('patch','other_session',{'pinned':True},access=ACCESS),'coordination_request_conflict')

    def test_replay_is_authorized_again_under_gate(self):
        self.patch();calls=[]
        def auth(*_):calls.append(1);return len(calls)==1
        self.rejects(lambda:self.patch(writer=commands(self.c,authorize=auth)),'coordination_access_denied')
        self.assertEqual(len(calls),2)

    def test_revocation_after_buffer_mutation_prevents_intent_and_publish(self):
        calls=[]
        def auth(*_):calls.append(1);return len(calls)<3
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.patch(writer=commands(self.c,authorize=auth)),'coordination_access_denied')
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_permission_loss_before_publish_leaves_explicit_recovery(self):
        calls=[]
        def auth(*_):calls.append(1);return len(calls)<4
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.patch(writer=commands(self.c,authorize=auth)),'coordination_access_denied')
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.patch(recover=True).generation,1)

    def test_recovery_rejects_another_body_or_principal_before_publish(self):
        self.pending();before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.patch({'pinned':False},recover=True),'coordination_request_conflict')
        self.rejects(lambda:self.patch(access=ConfigCommandAccess('other_owner'),recover=True),'coordination_request_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.patch(recover=True).generation,1)

    def test_recovery_rechecks_current_access_and_never_reexecutes_patch(self):
        self.pending()
        self.rejects(lambda:self.patch(writer=commands(self.c,authorize=lambda *_:False),recover=True),'coordination_access_denied')
        with patch.object(WebSessionProviderAuthority,'patch_session',side_effect=AssertionError('mutation repeated')):
            self.assertTrue(self.patch(recover=True).replayed)

    def test_fallback_change_conflicts_with_original_request_identity(self):
        self.writer.update_config('settings',{'history_n':3},access=ACCESS)
        self.writer._routes=lambda:[{**ROUTES[0],'key':'new synthetic secret'}]
        self.rejects(lambda:self.writer.update_config('settings',{'history_n':3},access=ACCESS),'coordination_request_conflict')

    def test_composite_actions_refuse_before_authorization_or_side_effects(self):
        writer=commands(None,authorize=lambda *_:self.fail('authorization reached'))
        for action in ('session.create.v1','session.publish.v1','session.fork.v1','session.delete.v1','provider.migrate.v1'):
            with self.subTest(action=action):
                self.rejects(lambda:writer.execute('id',action,CID,{},access=ACCESS),'coordination_action_unsupported')

    def test_invalid_non_mvp_coercion_does_not_expose_payload_in_error(self):
        self.writer.render_mvp=False
        self.rejects(lambda:self.writer.update_config('settings',{'history_n':'synthetic secret'},access=ACCESS),
                     'coordination_config_invalid')

    def child(self,mode):
        p=subprocess.Popen([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_commands import process_writer; process_writer(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if p.poll() is None:p.kill()
            p.communicate(timeout=5)
        self.addCleanup(cleanup);return p

    def test_two_actual_api_entry_processes_preserve_settings_and_session_metadata(self):
        first=self.child('settings')
        with selectors.DefaultSelector() as selector:
            selector.register(first.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=5))
            self.assertEqual(first.stdout.readline().strip(),'ready')
        second=self.child('patch')
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(timeout=.15)
        _,err1=first.communicate('\n',timeout=10);_,err2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),err1+err2)
        cfg=self.h.get('legacy');self.assertEqual(cfg['history_n'],77);self.assertEqual(cfg['sessions'][0]['title'],'process title')

    def test_process_death_after_intent_recovers_same_command_identity(self):
        child=self.child('crash');_,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,72,error)
        self.assertEqual(self.patch(recover=True).generation,1)
        self.assertTrue(self.h.get('legacy')['sessions'][0]['pinned'])

    def test_command_receipt_and_sql_journal_do_not_expose_route_secret(self):
        receipt=self.writer.update_config('settings',{'history_n':3},access=ACCESS)
        self.assertNotIn('synthetic secret',repr(receipt))
        self.assertNotIn('synthetic secret',repr(self.h.conn.execute('SELECT * FROM session_coordination_ops').fetchall()))
        self.assertFalse(self.writer.production_supported);self.assertFalse(self.writer.http_integrated)


if __name__=='__main__':unittest.main()
