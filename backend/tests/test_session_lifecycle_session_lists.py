"""Offline snapshot/CAS metadata merge through real save entry points."""
import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError
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

from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_session_lists import CoordinatedSessionListCommands, READ, SAVE, MAX_ROWS
from backend.web_session_provider_authority import WebSessionProviderAuthority, WebSessionProviderAuthorityError
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegration, CodexCanaryLoopIntegrationError
from backend.tests.test_codex_canary_loop_integration import FakeLegacy, FakeControl, FakeController
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

SID2='synthetic_second'
ACCESS=ConfigCommandAccess('synthetic_owner')


def session_lists(c, *, lock=None, authorize=lambda *_:True, history=None, enabled=True):
    return CoordinatedSessionListCommands(c,session_lock=lock or RLock(),authorize=authorize,
        historical_provider=history or (lambda sid:'codex' if sid==SID2 else None),enabled=enabled)


def api_save_entry():
    path=Path(__file__).resolve().parents[2]/'examples'/'api_loop.py'
    tree=ast.parse(path.read_text())
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='save_sessions']
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    class HTTPError(Exception):
        def __init__(self,*,status_code,detail):self.status_code,self.detail=status_code,detail
    namespace={'HTTPException':HTTPError}
    exec(compile(ast.fix_missing_locations(module),str(path),'exec'),namespace)
    return namespace


def packet(snapshot):
    rows=snapshot.as_rows();rows[0]['title']='new title'
    return {'rows':rows,'generation':snapshot.generation,'sha':snapshot.snapshot_sha}


def process_writer(path,mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        c=coordinator(path);writer=session_lists(c)
        if mode=='stale':
            request=json.loads(sys.stdin.readline())
        else:
            request=packet(writer.snapshot_sessions(access=ACCESS))
        if mode=='second':
            request['rows'][1]['pinned']=True
        if mode=='hold':
            calls=[]
            def authorize(_access,action,target):
                if action==SAVE and target is None:
                    calls.append(1)
                    if len(calls)==4:
                        print('ready',flush=True);sys.stdin.readline()
                return True
            writer._authorize=authorize
        def operation():
            return writer.save_sessions('second' if mode in {'second','stale'} else 'batch',request['rows'],
                access=ACCESS,expected_generation=request['generation'],expected_snapshot_sha=request['sha'])
        if mode.startswith('crash_'):
            from backend import deployment_config
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode=='crash_after':original(*args)
                os._exit(75)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):operation()
        else:
            try:result=operation()
            except LifecycleError as error:
                print(error.category,flush=True)
            else:print(result.generation,flush=True)


@unittest.skipUnless(os.name=='posix','private-file cooperative gate requires POSIX')
class SessionListTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        config=self.h.get('legacy');first=config['sessions'][0]
        first.pop('provider',None);first['since_id']='0';first['extension']={'synthetic_private':'preserve'}
        second=deepcopy(first);second.update(id=SID2,title='second title',provider='codex',since_id=0)
        config['sessions'].append(second);config['active_session']=CID
        config['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        config['deleted_sessions']=[{'id':'synthetic_deleted','provider':'api','deleted_at':'2026-11-05T00:00:00Z'}]
        self.h.put('legacy',config);(self.root/'gate').mkdir(mode=0o700)
        self.c=coordinator(self.h.path);self.c.bootstrap();self.writer=session_lists(self.c)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category)
        self.assertEqual(str(caught.exception),category)

    def snapshot(self,writer=None):return (writer or self.writer).snapshot_sessions(access=ACCESS)

    def save(self,request,*,writer=None,rid='batch',active=None,access=ACCESS,recover=False):
        return (writer or self.writer).save_sessions(rid,request['rows'],active,access=access,
            expected_generation=request['generation'],expected_snapshot_sha=request['sha'],recover=recover)

    def current(self):return self.h.get('legacy')
    def generation(self):return self.h.conn.execute('SELECT generation FROM session_coordination_meta').fetchone()[0]

    def test_default_off_and_denial_precede_io(self):
        def forbidden(*_):self.fail('dependency called')
        writer=CoordinatedSessionListCommands(None,session_lock=None,authorize=forbidden,historical_provider=forbidden)
        self.rejects(lambda:self.snapshot(writer),'lifecycle_disabled')
        self.rejects(lambda:self.save({'rows':[],'generation':0,'sha':'a'*64},writer=writer),'lifecycle_disabled')
        self.rejects(lambda:self.snapshot(session_lists(None,authorize=lambda *_:False)),'coordination_access_denied')

    def test_snapshot_is_immutable_scoped_and_excludes_config_secrets(self):
        before=self.current();snapshot=self.snapshot()
        self.assertEqual(snapshot.generation,0);self.assertEqual(snapshot.stored_active,CID)
        self.assertEqual([row.provider for row in snapshot.rows],['api','codex'])
        with self.assertRaises(FrozenInstanceError):snapshot.rows[0].title='changed'
        with self.assertRaises(FrozenInstanceError):snapshot.generation=9
        rows=snapshot.as_rows();rows[0]['title']='changed'
        self.assertEqual(self.current(),before);self.assertNotEqual(snapshot.as_rows()[0]['title'],'changed')
        for secret in ('synthetic secret','synthetic.invalid','synthetic_private'):
            self.assertNotIn(secret,repr(snapshot));self.assertNotIn(secret,repr(snapshot.as_rows()))

    def test_snapshot_has_no_json_database_or_intent_write(self):
        paths=(self.h.config_path,Path(self.h.path));before={p:p.read_bytes() for p in paths}
        self.snapshot()
        for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_observer_has_no_save_api_and_read_connections_are_query_only(self):
        def observe(legacy,generation):
            self.assertFalse(hasattr(legacy,'save_config'))
            changed=legacy.load_config();changed['secret']='changed'
            self.assertNotIn('secret',legacy.load_config())
            return generation
        self.assertEqual(self.c.observe_config(observe),0)
        with self.c._db(write=False) as conn:
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0],1)
            with self.assertRaises(__import__('sqlite3').OperationalError):conn.execute('DELETE FROM messages')

    def test_snapshot_requires_read_scope_for_every_session(self):
        writer=session_lists(self.c,authorize=lambda _a,_action,target:target!=SID2)
        self.rejects(lambda:self.snapshot(writer),'coordination_access_denied')

    def test_snapshot_rechecks_per_session_authorization_before_return(self):
        calls=[]
        def authorize(_a,action,target):
            if action==READ and target==SID2:calls.append(1)
            return len(calls)<2
        self.rejects(lambda:self.snapshot(session_lists(self.c,authorize=authorize)),'coordination_access_denied')

    def test_bulk_merge_changes_only_metadata_and_keeps_raw_extensions(self):
        before=self.current();request=packet(self.snapshot());request['rows'][1]['pinned']=True
        source=self.h.conn.execute('SELECT * FROM messages').fetchall()
        receipt=self.save(request,active=SID2)
        expected=deepcopy(before);expected['sessions'][0]['title']='new title'
        expected['sessions'][1]['pinned']=True;expected['active_session']=SID2
        self.assertEqual(self.current(),expected);self.assertEqual(receipt.generation,1)
        self.assertNotIn('provider',self.current()['sessions'][0]);self.assertEqual(self.current()['sessions'][0]['since_id'],'0')
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(),source)

    def test_real_authority_save_entry_delegates_to_metadata_command(self):
        request=packet(self.snapshot());authority=WebSessionProviderAuthority(self.h)
        receipt=authority.save_sessions(request['rows'],coordinated=self.writer,request_id='batch',access=ACCESS,
            expected_generation=request['generation'],expected_snapshot_sha=request['sha'])
        self.assertEqual(receipt.generation,1);self.assertEqual(self.current()['sessions'][0]['title'],'new title')

    def test_real_api_save_function_explicit_and_default_branches(self):
        request=packet(self.snapshot());api=api_save_entry()
        result=api['save_sessions'](request['rows'],coordinated=self.writer,request_id='batch',access=ACCESS,
            expected_generation=request['generation'],expected_snapshot_sha=request['sha'])
        self.assertEqual(result.generation,1)
        state={'sessions':[],'unrelated':'kept'}
        api.update(load_config=lambda:deepcopy(state),save_config=lambda value:state.update(value),sessions_public=lambda:deepcopy(state))
        result=api['save_sessions']([{'id':'synthetic'}],'synthetic')
        self.assertEqual(result,{'sessions':[{'id':'synthetic'}],'active_session':'synthetic','unrelated':'kept'})

    def test_real_canary_wrapper_requires_the_same_existing_lock(self):
        legacy=FakeLegacy();runtime=SimpleNamespace(controller=FakeController(),control=FakeControl(),generation_enabled=False)
        integration=CodexCanaryLoopIntegration(legacy,runtime);integration.install_legacy_globals()
        writer=session_lists(self.c,lock=integration._session_lock);request=packet(self.snapshot(writer))
        kwargs={'request_id':'batch','access':ACCESS,'expected_generation':request['generation'],'expected_snapshot_sha':request['sha']}
        self.assertEqual(legacy.save_sessions(request['rows'],coordinated=writer,**kwargs).generation,1)
        self.rejects(lambda:legacy.save_sessions(request['rows'],coordinated=self.writer,**kwargs),'coordination_lock_mismatch')

    def test_metadata_without_dependency_cannot_fall_back_to_default_save(self):
        request=packet(self.snapshot());before=self.h.config_path.read_bytes()
        with self.assertRaises(WebSessionProviderAuthorityError):
            WebSessionProviderAuthority(self.h).save_sessions(request['rows'],request_id='id')
        api=api_save_entry();api['load_config']=lambda:self.fail('legacy IO')
        with self.assertRaises(api['HTTPException']) as caught:api['save_sessions'](request['rows'],recover=True)
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_stale_snapshot_does_not_overwrite_newer_patch(self):
        request=packet(self.snapshot())
        config_commands(self.c).patch_session('patch',CID,{'title':'newer title'},access=ACCESS)
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.save(request),'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_old_metadata_digest_still_refuses_if_generation_is_updated(self):
        request=packet(self.snapshot())
        config_commands(self.c).patch_session('patch',CID,{'title':'newer title'},access=ACCESS)
        request['generation']=1
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.save(request),'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_unrelated_setting_change_also_invalidates_generation(self):
        request=packet(self.snapshot());config_commands(self.c).update_config('settings',{'history_n':77},access=ACCESS)
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.save(request),'coordination_cas_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_wrong_digest_refuses_without_intent(self):
        request=packet(self.snapshot());request['sha']='a'*64
        self.rejects(lambda:self.save(request),'coordination_cas_conflict')
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_add_remove_reorder_and_immutable_changes_are_unsupported(self):
        original=packet(self.snapshot());cases=[]
        value=deepcopy(original);value['rows'].pop();cases.append(value)
        value=deepcopy(original);row=deepcopy(value['rows'][0]);row['id']='synthetic_new';value['rows'].append(row);cases.append(value)
        value=deepcopy(original);value['rows'].reverse();cases.append(value)
        for key,change in (('id','synthetic_other'),('provider','codex'),('since_id',9),('created_at','changed')):
            value=deepcopy(original);value['rows'][0][key]=change;cases.append(value)
        before=self.h.config_path.read_bytes()
        for request in cases:
            with self.subTest(rows=request['rows']):self.rejects(lambda:self.save(request),'coordination_session_topology_unsupported')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_invalid_row_types_duplicates_unknown_keys_and_limits_refuse(self):
        original=packet(self.snapshot());cases=[]
        for key,value in (('pinned','false'),('since_id',False),('title',None),('provider','other')):
            request=deepcopy(original);request['rows'][0][key]=value;cases.append(request)
        request=deepcopy(original);request['rows'][0]['unexpected']='synthetic secret';cases.append(request)
        request=deepcopy(original);request['rows'][1]=deepcopy(request['rows'][0]);cases.append(request)
        request=deepcopy(original);request['rows']=request['rows']*(MAX_ROWS+1);cases.append(request)
        for request in cases:
            with self.subTest(count=len(request['rows'])):self.rejects(lambda:self.save(request),'coordination_sessions_invalid')

    def test_active_pointer_must_be_existing_and_none_preserves_it(self):
        request=packet(self.snapshot())
        self.rejects(lambda:self.save(request,active='missing'),'coordination_sessions_invalid')
        self.save(request);self.assertEqual(self.current()['active_session'],CID)

    def test_missing_active_field_is_recorded_and_preserved_without_fallback(self):
        self.c.mutate('remove_active',{},lambda legacy:legacy.save_config({k:v for k,v in legacy.load_config().items() if k!='active_session'}))
        snapshot=self.snapshot();self.assertIsNone(snapshot.stored_active)
        self.save(packet(snapshot));self.assertNotIn('active_session',self.current())

    def test_unknown_stored_active_refuses_snapshot(self):
        def modify(legacy):config=legacy.load_config();config['active_session']='missing';legacy.save_config(config)
        self.c.mutate('invalid_active',{},modify)
        self.rejects(self.snapshot,'coordination_sessions_invalid')

    def test_snapshot_refuses_more_than_bounded_session_count(self):
        def modify(legacy):
            config=legacy.load_config();template=deepcopy(config['sessions'][0])
            config['sessions']=[{**template,'id':f'synthetic_{index}'} for index in range(MAX_ROWS+1)]
            config['active_session']='synthetic_0';legacy.save_config(config)
        self.c.mutate('seed_many',{},modify)
        self.rejects(self.snapshot,'coordination_sessions_invalid')

    def test_save_requires_scope_for_all_rows_and_rechecks_grants(self):
        request=packet(self.snapshot());writer=session_lists(self.c,authorize=lambda _a,_action,target:target!=SID2)
        self.rejects(lambda:self.save(request,writer=writer),'coordination_access_denied')
        globals_seen=[]
        def revoke(_a,action,target):
            if action==SAVE and target is None:globals_seen.append(1)
            return len(globals_seen)<4
        before=self.h.config_path.read_bytes()
        self.rejects(lambda:self.save(request,writer=session_lists(self.c,authorize=revoke)),'coordination_access_denied')
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_provider_history_change_and_history_errors_refuse(self):
        request=packet(self.snapshot())
        self.rejects(lambda:self.save(request,writer=session_lists(self.c,history=lambda _: 'codex')),'coordination_session_changed')
        def broken(_):raise RuntimeError('synthetic secret /private/path')
        self.rejects(lambda:self.snapshot(session_lists(self.c,history=broken)),'coordination_history_unavailable')

    def test_same_id_replay_is_historical_and_binds_rows_active_actor_and_digest(self):
        request=packet(self.snapshot());self.save(request)
        config_commands(self.c).patch_session('later',CID,{'title':'later title'},access=ACCESS)
        self.assertTrue(self.save(request).replayed);self.assertEqual(self.current()['sessions'][0]['title'],'later title')
        changed=deepcopy(request);changed['rows'][0]['title']='different'
        self.rejects(lambda:self.save(changed),'coordination_request_conflict')
        self.rejects(lambda:self.save(request,active=SID2),'coordination_request_conflict')
        self.rejects(lambda:self.save(request,access=ConfigCommandAccess('other_owner')),'coordination_request_conflict')
        changed=deepcopy(request);changed['sha']='a'*64
        self.rejects(lambda:self.save(changed),'coordination_request_conflict')

    def test_pending_blocks_snapshot_and_recovery_does_not_rerun_merge(self):
        request=packet(self.snapshot())
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.rejects(lambda:self.save(request),'coordination_unavailable')
        self.rejects(self.snapshot,'coordination_recovery_required')
        changed=deepcopy(request);changed['rows'][0]['title']='changed'
        self.rejects(lambda:self.save(changed,recover=True),'coordination_request_conflict')
        with patch.object(self.writer,'_projection',side_effect=AssertionError('merge replay')):
            self.assertEqual(self.save(request,recover=True).generation,1)

    def test_pending_recovery_requires_current_access_and_provider_history(self):
        request=packet(self.snapshot())
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.rejects(lambda:self.save(request),'coordination_unavailable')
        self.rejects(lambda:self.save(request,writer=session_lists(self.c,authorize=lambda *_:False),recover=True),'coordination_access_denied')
        self.rejects(lambda:self.save(request,writer=session_lists(self.c,history=lambda _:'codex'),recover=True),'coordination_session_changed')
        self.assertTrue(self.save(request,recover=True).replayed)

    def test_external_json_edits_refuse_observation_and_write(self):
        request=packet(self.snapshot());config=self.current();config['external']=True;self.h.put('legacy',config)
        before=self.h.config_path.read_bytes()
        self.rejects(self.snapshot,'coordination_external_change')
        self.rejects(lambda:self.save(request),'coordination_external_change')
        self.assertEqual(self.h.config_path.read_bytes(),before)

    def test_snapshot_detects_external_edit_inside_observer(self):
        def observe(_legacy,_generation):
            config=self.current();config['external']=True;self.h.put('legacy',config)
            return 'must not return'
        self.rejects(lambda:self.c.observe_config(observe),'coordination_external_change')

    def test_sql_journal_and_receipt_do_not_contain_metadata_plaintext(self):
        request=packet(self.snapshot());result=self.save(request)
        rows=self.h.conn.execute('SELECT * FROM session_coordination_ops').fetchall()
        for value in ('new title','second title','synthetic secret','synthetic_private'):
            self.assertNotIn(value,repr(rows));self.assertNotIn(value,repr(result))
        self.assertFalse(self.writer.production_supported);self.assertFalse(self.writer.http_integrated)

    def child(self,mode):
        process=subprocess.Popen([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_session_lists import process_writer; process_writer(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup);return process

    def hold(self):
        first=self.child('hold')
        with selectors.DefaultSelector() as selector:
            selector.register(first.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=5))
            self.assertEqual(first.stdout.readline().strip(),'ready')
        return first

    def test_process_read_waits_for_writer_then_preserves_both_changes(self):
        first=self.hold();second=self.child('second')
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(timeout=.15)
        _,error1=first.communicate('\n',timeout=10);_,error2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),error1+error2)
        self.assertEqual(self.current()['sessions'][0]['title'],'new title')
        self.assertTrue(self.current()['sessions'][1]['pinned']);self.assertEqual(self.generation(),2)

    def test_process_with_old_snapshot_cannot_overwrite_winning_writer(self):
        request=packet(self.snapshot());first=self.hold();second=self.child('stale')
        with self.assertRaises(subprocess.TimeoutExpired):second.communicate(json.dumps(request)+'\n',timeout=.15)
        _,error1=first.communicate('\n',timeout=10);out2,error2=second.communicate(timeout=10)
        self.assertEqual((first.returncode,second.returncode),(0,0),error1+error2)
        self.assertEqual(out2.strip(),'coordination_cas_conflict');self.assertEqual(self.generation(),1)

    def test_process_crash_before_publish_recovers_from_original_packet(self):
        request=packet(self.snapshot());before=self.h.config_path.read_bytes()
        child=self.child('crash_before');_,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,75,error);self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.save(request,recover=True).generation,1)

    def test_process_crash_after_publish_finalizes_without_republish(self):
        request=packet(self.snapshot());child=self.child('crash_after');_,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,75,error)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('republish')):
            self.assertEqual(self.save(request,recover=True).generation,1)
        self.assertEqual(self.current()['sessions'][0]['title'],'new title')


if __name__=='__main__':unittest.main()
