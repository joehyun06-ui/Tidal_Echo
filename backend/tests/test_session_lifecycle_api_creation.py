"""Synthetic-only fresh API creation; no real user chat/native/model operation."""
import ast
import asyncio
from copy import deepcopy
import datetime as dt
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

from backend import api_web_generation, deployment_config
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_api_creation import CoordinatedApiSessionCommands, CREATE, PUBLISH
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.web_session_provider_authority import WebSessionProviderAuthority, WebSessionProviderAuthorityError
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegration, CodexCanaryLoopIntegrationError
from backend.tests.test_codex_canary_loop_integration import FakeLegacy, FakeControl, FakeController
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

TARGET='api-synthetic-new'
STAMP='2026-11-05T12:00:00+00:00'
TITLE='synthetic created title'
ACCESS=ConfigCommandAccess('synthetic_owner')


def creator(c,*,lock=None,authorize=lambda *_:True,history=lambda _:None,enabled=True):
    return CoordinatedApiSessionCommands(c,session_lock=lock or RLock(),authorize=authorize,
        historical_provider=history,enabled=enabled)


def new_row():
    return {'id':TARGET,'title':TITLE,'since_id':0,'created_at':STAMP,'pinned':False,'provider':'api'}


def create_entry(path):
    source=Path(__file__).resolve().parents[2]/'examples'/'api_loop.py'
    nodes=[n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='create_session']
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    class HTTPError(Exception):
        def __init__(self,*,status_code,detail):self.status_code,self.detail=status_code,detail
    namespace={'HTTPException':HTTPError,'LOOP_CONFIG':path,'dt':dt,
        'uuid':SimpleNamespace(uuid4=lambda:SimpleNamespace(hex='0'*32)),'now_iso':lambda:'legacy timestamp'}
    exec(compile(ast.fix_missing_locations(module),str(source),'exec'),namespace)
    return namespace


def packet(snapshot):
    return {'title':TITLE,'session_id':TARGET,'created_at':STAMP,'request_id':'create',
        'expected_generation':snapshot.generation,'expected_snapshot_sha':snapshot.snapshot_sha}


def process_writer(path,mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        c=coordinator(path);writer=creator(c)
        if mode in {'stale','replay'}:request=json.loads(sys.stdin.readline())
        else:request=packet(writer.snapshot_sessions(access=ACCESS))
        if mode=='hold':
            calls=[]
            def authorize(_access,action,target):
                if action==CREATE and target is None:
                    calls.append(1)
                    if len(calls)==4:print('ready',flush=True);sys.stdin.readline()
                return True
            writer._authorize=authorize
        if mode=='stale':request['request_id']='second'
        operation=lambda:writer.create_api_session(access=ACCESS,**request)
        if mode.startswith('crash_'):
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode=='crash_after':original(*args)
                os._exit(76)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):operation()
        else:
            try:result=operation()
            except LifecycleError as error:print(error.category,flush=True)
            else:print(json.dumps({'generation':result.generation,'replayed':result.replayed}),flush=True)


@unittest.skipUnless(os.name=='posix','private JSON coordinator requires POSIX')
class ApiCreationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        self.h.LOOP_CONFIG=self.h.config_path
        config=self.h.get('legacy');config['active_session']=CID;config['sessions'][0]['extension']={'synthetic':'preserved'}
        config['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        config['deleted_sessions']=[{'id':'api-synthetic-deleted','provider':'api','deleted_at':STAMP}]
        self.h.put('legacy',config);(self.root/'gate').mkdir(mode=0o700)
        self.c=coordinator(self.h.path);self.c.bootstrap();self.writer=creator(self.c)
        self.request=packet(self.writer.snapshot_sessions(access=ACCESS))
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)

    def create(self,*,writer=None,request=None,access=ACCESS,recover=False,activate=True):
        return (writer or self.writer).create_api_session(access=access,recover=recover,activate=activate,
            **(self.request if request is None else request))

    def publish(self,*,writer=None,row=None,rid='publish',access=ACCESS,recover=False):
        return (writer or self.writer).publish_row(new_row() if row is None else row,activate=True,
            request_id=rid,access=access,expected_generation=self.request['expected_generation'],
            expected_snapshot_sha=self.request['expected_snapshot_sha'],recover=recover)

    def current(self):return self.h.get('legacy')
    def generation(self):return self.h.conn.execute('SELECT generation FROM session_coordination_meta').fetchone()[0]

    def add_message(self,sid=TARGET):
        return self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','synthetic input',?)",
            (STAMP,json.dumps({'api_session':sid}))).lastrowid

    def test_default_off_and_denial_precede_dependencies(self):
        def forbidden(*_):self.fail('dependency used')
        disabled=CoordinatedApiSessionCommands(None,session_lock=None,authorize=forbidden,historical_provider=forbidden)
        self.rejects(lambda:self.create(writer=disabled),'lifecycle_disabled')
        denied=creator(None,authorize=lambda *_:False)
        self.rejects(lambda:self.create(writer=denied),'coordination_access_denied')

    def test_creation_appends_only_fixed_row_and_preserves_other_config_and_messages(self):
        before=self.current();messages=self.h.conn.execute('SELECT * FROM messages').fetchall()
        with patch.object(WebSessionProviderAuthority,'new_row',side_effect=AssertionError('clock/ID allocation')):
            receipt=self.create()
        expected=deepcopy(before);expected['sessions'].append(new_row());expected['active_session']=TARGET
        self.assertEqual(self.current(),expected);self.assertEqual((receipt.generation,receipt.replayed),(1,False))
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(),messages)

    def test_activate_false_preserves_stored_pointer(self):
        self.create(activate=False);self.assertEqual(self.current()['active_session'],CID)

    def test_publish_entry_is_idempotent_and_does_not_allocate_identity(self):
        authority=WebSessionProviderAuthority(self.h)
        kwargs={'activate':True,'coordinated':self.writer,'request_id':'publish','access':ACCESS,
            'expected_generation':0,'expected_snapshot_sha':self.request['expected_snapshot_sha']}
        result=authority.publish_row(new_row(),**kwargs)
        self.assertEqual(result.generation,1);self.assertTrue(authority.publish_row(new_row(),**kwargs).replayed)
        self.assertEqual(len(self.current()['sessions']),2)

    def test_real_authority_create_entry_uses_stable_packet(self):
        result=WebSessionProviderAuthority(self.h).create_api_session(coordinated=self.writer,access=ACCESS,**self.request)
        self.assertEqual(result.generation,1);self.assertEqual(self.current()['sessions'][-1],new_row())

    def test_real_api_create_function_explicit_and_default_paths(self):
        api=create_entry(self.h.config_path)
        self.assertEqual(api['create_session'](coordinated=self.writer,access=ACCESS,**self.request).generation,1)
        state={'rows':[]}
        api['session_rows']=lambda:deepcopy(state['rows'])
        api['save_sessions']=lambda rows,active:state.update(rows=deepcopy(rows),active=active)
        row=api['create_session']('legacy title')
        self.assertEqual(row['title'],'legacy title');self.assertEqual(row['created_at'],'legacy timestamp')
        self.assertEqual(state['active'],row['id'])

    def integration(self):
        legacy=FakeLegacy();legacy.LOOP_CONFIG=self.h.config_path
        runtime=SimpleNamespace(controller=FakeController(),control=FakeControl(),generation_enabled=True)
        integration=CodexCanaryLoopIntegration(legacy,runtime);integration.install_legacy_globals()
        return legacy,runtime,integration,creator(self.c,lock=integration._session_lock)

    def test_canary_legacy_wrapper_binds_existing_lock(self):
        legacy,runtime,integration,writer=self.integration()
        self.assertEqual(legacy.create_session(coordinated=writer,access=ACCESS,**self.request).generation,1)
        self.assertEqual(runtime.controller.calls,[])
        self.rejects(lambda:legacy.create_session(coordinated=self.writer,access=ACCESS,**self.request),'coordination_lock_mismatch')

    def test_async_api_entry_never_invokes_native_controller(self):
        _,runtime,integration,writer=self.integration()
        result=asyncio.run(integration.create_web_session(provider='api',coordinated=writer,access=ACCESS,**self.request))
        self.assertEqual(result.generation,1);self.assertEqual(runtime.controller.calls,[])

    def test_codex_async_and_native_publish_refuse_before_pin(self):
        _,runtime,integration,writer=self.integration()
        with self.assertRaises(CodexCanaryLoopIntegrationError) as caught:
            asyncio.run(integration.create_web_session(provider='codex',coordinated=writer,access=ACCESS,**self.request))
        self.assertEqual(caught.exception.category,'coordination_native_creation_unsupported')
        row=new_row();row['provider']='codex'
        self.rejects(lambda:self.publish(row=row),'coordination_native_creation_unsupported')
        self.assertEqual(runtime.controller.calls,[]);self.assertEqual(self.generation(),0)

    def test_entry_rejects_wrong_config_dependency(self):
        api=create_entry(self.root/'wrong.json')
        self.rejects(lambda:api['create_session'](coordinated=self.writer,access=ACCESS,**self.request),'coordination_resource_mismatch')
        self.h.LOOP_CONFIG=self.root/'wrong.json'
        self.rejects(lambda:WebSessionProviderAuthority(self.h).create_api_session(coordinated=self.writer,access=ACCESS,**self.request),
            'coordination_resource_mismatch')

    def test_candidate_metadata_without_dependency_never_falls_back(self):
        before=self.h.config_path.read_bytes();authority=WebSessionProviderAuthority(self.h)
        with self.assertRaises(WebSessionProviderAuthorityError):authority.create_api_session(request_id='id')
        with self.assertRaises(WebSessionProviderAuthorityError):authority.publish_row(new_row(),activate=True,recover=True)
        api=create_entry(self.h.config_path);api['session_rows']=lambda:self.fail('legacy IO')
        with self.assertRaises(api['HTTPException']):api['create_session'](request_id='id')
        _,runtime,integration,_=self.integration()
        with self.assertRaises(CodexCanaryLoopIntegrationError):
            asyncio.run(integration.create_web_session(provider='codex',request_id='id'))
        self.assertEqual(runtime.controller.calls,[]);self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_missing_allocation_timestamp_or_identity_is_rejected(self):
        for fields in ({'session_id':None},{'created_at':None},{'created_at':'legacy timestamp'},
                       {'created_at':'2026-11-05T12:00:00'},{'created_at':'2026-11-05T12:00:00+01:00'}):
            request={**self.request,**fields}
            with self.subTest(fields=fields):self.rejects(lambda:self.create(request=request),'coordination_creation_invalid')

    def test_nonempty_history_and_extra_row_fields_are_unsupported(self):
        request={**self.request,'since_id':1}
        self.rejects(lambda:self.create(request=request),'coordination_creation_scope_unsupported')
        row=new_row();row['pinned']=True
        self.rejects(lambda:self.publish(row=row),'coordination_creation_scope_unsupported')
        row=new_row();row['branch_origin']='synthetic'
        self.rejects(lambda:self.publish(row=row),'coordination_creation_invalid')

    def test_create_requires_global_and_target_scope(self):
        writer=creator(self.c,authorize=lambda _a,_action,target:target is None)
        self.rejects(lambda:self.create(writer=writer),'coordination_access_denied')
        self.assertEqual(self.generation(),0)

    def test_creation_grant_is_rechecked_before_intent(self):
        calls=[]
        def authorize(_a,action,target):
            if action==CREATE and target is None:calls.append(1)
            return len(calls)<4
        self.rejects(lambda:self.create(writer=creator(self.c,authorize=authorize)),'coordination_access_denied')
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_stale_generation_and_stale_digest_cannot_append(self):
        config_commands(self.c).update_config('settings',{'history_n':77},access=ACCESS)
        before=self.h.config_path.read_bytes()
        self.rejects(self.create,'coordination_cas_conflict')
        request={**self.request,'expected_generation':1,'expected_snapshot_sha':'a'*64}
        self.rejects(lambda:self.create(request=request),'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_replay_is_historical_after_later_metadata_and_history(self):
        self.create();self.add_message()
        config_commands(self.c).patch_session('later',TARGET,{'title':'later title'},access=ACCESS)
        writer=creator(self.c,history=lambda sid:'api' if sid==TARGET else None)
        result=self.create(writer=writer)
        self.assertTrue(result.replayed);self.assertEqual(result.generation,1)
        self.assertEqual(self.current()['sessions'][-1]['title'],'later title')
        self.assertEqual(len(self.current()['sessions']),2)

    def test_request_identity_binds_allocation_actor_action_and_activation(self):
        self.create()
        for fields in ({'created_at':'2026-11-05T13:00:00Z'},{'session_id':'api-other'},{'title':'other title'}):
            request={**self.request,**fields}
            with self.subTest(fields=fields):self.rejects(lambda:self.create(request=request),'coordination_request_conflict')
        self.rejects(lambda:self.create(access=ConfigCommandAccess('other_owner')),'coordination_request_conflict')
        self.rejects(lambda:self.create(activate=False),'coordination_request_conflict')
        self.rejects(lambda:self.publish(rid='create'),'coordination_request_conflict')

    def test_live_row_tombstone_and_historical_id_cannot_be_reused(self):
        for sid in (CID,'api-synthetic-deleted'):
            request={**self.request,'session_id':sid}
            with self.subTest(sid=sid),self.assertRaises(LifecycleError):self.create(request=request)
        writer=creator(self.c,history=lambda sid:'api' if sid==TARGET else None)
        self.rejects(lambda:self.create(writer=writer),'coordination_session_conflict')
        writer=creator(self.c,history=lambda sid:'codex' if sid==TARGET else None)
        self.rejects(lambda:self.create(writer=writer),'coordination_session_changed')

    def test_fresh_request_cannot_duplicate_visible_row(self):
        self.create();snapshot=self.writer.snapshot_sessions(access=ACCESS)
        request={**packet(snapshot),'request_id':'another'}
        self.rejects(lambda:self.create(request=request),'coordination_session_conflict')

    def test_orphan_messages_and_version_references_are_not_fresh(self):
        message=self.add_message();self.rejects(self.create,'coordination_session_namespace_conflict')
        self.h.conn.execute('DELETE FROM messages WHERE id=?',(message,))
        self.h.conn.execute('INSERT INTO web_message_versions(version_id,root_id,parent_id) VALUES (?,?,?)',('synthetic_version',CID,TARGET))
        self.rejects(self.create,'coordination_session_namespace_conflict')

    def test_fork_context_and_old_lifecycle_records_cannot_be_reused(self):
        self.h.conn.execute('CREATE TABLE web_session_forks(target TEXT PRIMARY KEY,fingerprint TEXT)')
        self.h.conn.execute('INSERT INTO web_session_forks VALUES (?,?)',(TARGET,'a'*64))
        self.rejects(self.create,'coordination_session_namespace_conflict')
        self.h.conn.execute('DELETE FROM web_session_forks')
        self.h.conn.execute('CREATE TABLE web_session_context(root_id TEXT PRIMARY KEY,payload TEXT)')
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,?)',(TARGET,'{}'))
        self.rejects(self.create,'coordination_session_namespace_conflict')
        self.h.conn.execute('DELETE FROM web_session_context')
        self.h.conn.execute('INSERT INTO session_lifecycle_records VALUES (?,1,?,?)',(TARGET,'{}','a'*64))
        self.rejects(self.create,'coordination_session_namespace_conflict')

    def test_closed_api_session_and_orphan_job_cannot_be_reused(self):
        store=api_web_generation.Store(self.h.path);store.initialize()
        self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(TARGET,))
        self.rejects(self.create,'coordination_session_namespace_conflict')
        self.h.conn.execute('DELETE FROM api_web_closed_sessions')
        row=store.accept(TARGET,'synthetic orphan',{'api_session':TARGET})
        self.h.conn.execute('DELETE FROM messages WHERE id=?',(row['id'],))
        self.rejects(self.create,'coordination_session_namespace_conflict')

    def test_namespace_schema_and_malformed_message_metadata_fail_closed(self):
        self.h.conn.execute("UPDATE messages SET meta='not-json'")
        self.rejects(self.create,'coordination_namespace_invalid')
        self.h.conn.execute('UPDATE messages SET meta=?',(json.dumps({'api_session':CID}),))
        self.h.conn.execute('CREATE VIEW web_session_forks AS SELECT 1 AS target')
        self.rejects(self.create,'coordination_namespace_invalid')

    def test_namespace_is_rechecked_after_buffer_mutation_before_intent(self):
        original=self.c.validate
        def validate(config):original(config);self.add_message()
        self.c.validate=validate
        self.rejects(self.create,'coordination_session_namespace_conflict')
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)
        self.assertEqual(len(self.current()['sessions']),1)

    def test_pending_prepublication_creation_revalidates_canonical_namespace(self):
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.rejects(self.create,'coordination_unavailable')
        self.add_message();before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.create(recover=True),'coordination_session_namespace_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_recovery_after_publication_can_finish_without_claiming_current_emptiness(self):
        original=deployment_config.atomic_write_text
        def publish(*args):original(*args);raise OSError('lost publish acknowledgement')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):
            self.rejects(self.create,'coordination_unavailable')
        self.add_message();writer=creator(self.c,history=lambda sid:'api' if sid==TARGET else None)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('republish')):
            self.assertEqual(self.create(writer=writer,recover=True).generation,1)
        self.assertEqual(len(self.current()['sessions']),2)

    def test_pending_recovery_requires_original_packet_and_current_access(self):
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.rejects(self.create,'coordination_unavailable')
        request={**self.request,'session_id':'api-new-guess'}
        self.rejects(lambda:self.create(request=request,recover=True),'coordination_request_conflict')
        self.rejects(lambda:self.create(writer=creator(self.c,authorize=lambda *_:False),recover=True),'coordination_access_denied')
        with patch.object(self.writer,'_projection',side_effect=AssertionError('recreate callback')):
            self.assertTrue(self.create(recover=True).replayed)

    def test_journal_and_receipt_do_not_store_row_or_route_plaintext(self):
        receipt=self.create();rows=self.h.conn.execute('SELECT * FROM session_coordination_ops').fetchall()
        for value in (TITLE,STAMP,'synthetic secret','synthetic.invalid'):
            self.assertNotIn(value,repr(rows));self.assertNotIn(value,repr(receipt))
        self.assertFalse(self.writer.native_integrated);self.assertFalse(self.writer.http_integrated)

    def child(self,mode):
        process=subprocess.Popen([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_api_creation import process_writer; process_writer(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup);return process

    def test_processes_with_one_allocation_create_exactly_once(self):
        first=self.child('hold')
        with selectors.DefaultSelector() as selector:
            selector.register(first.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=5))
            self.assertEqual(first.stdout.readline().strip(),'ready')
        second=self.child('stale');request=deepcopy(self.request);request['request_id']='create'
        # A different request with the same old base must lose; it is not replay.
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(json.dumps(request)+'\n',timeout=.15)
        _,error1=first.communicate('\n',timeout=10);out2,error2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),error1+error2)
        self.assertEqual(out2.strip(),'coordination_cas_conflict')
        self.assertEqual(len(self.current()['sessions']),2);self.assertEqual(self.generation(),1)

    def test_concurrent_same_request_returns_historical_receipt_without_duplicate(self):
        first=self.child('hold')
        with selectors.DefaultSelector() as selector:
            selector.register(first.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=5))
            self.assertEqual(first.stdout.readline().strip(),'ready')
        second=self.child('replay')
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(json.dumps(self.request)+'\n',timeout=.15)
        _,error1=first.communicate('\n',timeout=10);out2,error2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),error1+error2)
        self.assertEqual(json.loads(out2),{'generation':1,'replayed':True})
        self.assertEqual(len(self.current()['sessions']),2);self.assertEqual(self.generation(),1)

    def test_process_exit_before_publish_recovers_exact_same_allocation(self):
        before=self.h.config_path.read_bytes();child=self.child('crash_before');_,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,76,error);self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.create(recover=True).generation,1)
        self.assertEqual(self.current()['sessions'][-1],new_row())

    def test_process_exit_after_publish_finalizes_without_duplicate_or_republish(self):
        child=self.child('crash_after');_,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,76,error)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('republish')):
            self.assertEqual(self.create(recover=True).generation,1)
        self.assertEqual(len(self.current()['sessions']),2)


if __name__=='__main__':unittest.main()
