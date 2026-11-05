"""Synthetic API selection metadata, SQL atomic receipts and process replay."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import selectors
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import api_web_generation, web_message_versions as versions
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_version_selection import CoordinatedVersionSelectionCommands,READ,SELECT
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.web_session_fork import fork_conversation,ForkError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

ACCESS=ConfigCommandAccess('synthetic_owner')
STAMP='2026-11-05T15:20:00Z'
OUT='api-synthetic-unrelated'
UUIDS=['12345678-1234-4234-8234-123456789abc','22345678-1234-4234-8234-123456789abc','32345678-1234-4234-8234-123456789abc']
A,B,C=['api-fork-'+u.replace('-','') for u in UUIDS]


def writer(c,path,*,authorize=lambda *_:True,history=lambda _:None,enabled=True):
    return CoordinatedVersionSelectionCommands(c,canonical_path=path,session_lock=RLock(),authorize=authorize,
        historical_provider=history,enabled=enabled)


def worker(path,mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        request=json.loads(sys.stdin.readline());c=coordinator(path);w=writer(c,path)
        if mode=='hold':
            inspect=w._inspect
            def hold(*args):value=inspect(*args);print('ready',flush=True);sys.stdin.readline();return value
            w._inspect=hold
        if mode=='crash_before':
            connect=c._connect
            class Proxy:
                def __init__(self,conn):self.conn=conn
                def __getattr__(self,n):return getattr(self.conn,n)
                def execute(self,sql,*args):
                    value=self.conn.execute(sql,*args)
                    if sql.startswith('INSERT INTO web_version_selections'):os._exit(92)
                    return value
            c._connect=lambda:Proxy(connect())
        if mode=='crash_after':
            unlink=Path.unlink
            def stop(p,*args,**kwargs):
                if p.name.startswith('config-') and p.suffix=='.stage':os._exit(93)
                return unlink(p,*args,**kwargs)
            Path.unlink=stop
        try:receipt=w.select_version(CID,{'version_id':A},request_id='stale' if mode=='stale' else 'select',access=ACCESS,**request)
        except LifecycleError as e:print(e.category,flush=True)
        else:print(json.dumps({'generation':receipt.generation,'replayed':receipt.replayed}),flush=True)


@unittest.skipUnless(os.name=='posix','POSIX cooperative gate')
class VersionSelectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        config=self.h.get('legacy');config['active_session']=CID;config['sessions'][0]['extension']={'preserve':True}
        config['sessions'].append({'id':OUT,'title':'unrelated','provider':'api'})
        config['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        self.h.put('legacy',config)
        for mid,direction,kind in [(2,'out','reply'),(3,'in','user'),(4,'out','reply')]:
            self.h.conn.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                (mid,STAMP,direction,kind,'synthetic private text',json.dumps({'api_session':CID})))
        self.legacy=SimpleNamespace(LOOP_CONFIG=self.h.config_path,load_config=lambda:self.h.get('legacy'),
            save_config=lambda cfg:self.h.put('legacy',cfg),now_iso=lambda:STAMP)
        self.authority=WebSessionProviderAuthority(self.legacy)
        # Real API prepare/copy/finish seed existing nested alternatives before
        # coordinator bootstrap. The new candidate never creates or repairs them.
        for i,sid in enumerate((CID,CID,A)):
            mid=4 if sid==CID else self.h.conn.execute("SELECT MAX(id) FROM messages WHERE json_extract(meta,'$.api_session')=?",(sid,)).fetchone()[0]
            body={'request_id':UUIDS[i],'message_id':mid,'mode':'regenerate'}
            versions.prepare(self.authority,sid,body,relay_db=self.h.path)
            fork_conversation(self.authority,sid,body,relay_db=self.h.path)
            versions.finish(self.authority,(A,B,C)[i],relay_db=self.h.path)
        (self.root/'gate').mkdir(mode=0o700);self.c=coordinator(self.h.path);self.c.bootstrap()
        self.w=writer(self.c,self.h.path);self.request=self.snapshot()
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def snapshot(self,target=A):return asdict(self.w.snapshot_selection(CID,{'version_id':target},access=ACCESS))
    def select(self,*,target=A,request=None,rid='select',w=None,access=ACCESS,recover=False):
        return (w or self.w).select_version(CID,{'version_id':target},request_id=rid,access=access,
            recover=recover,**(self.request if request is None else request))
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def selected(self):
        row=self.h.conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?',(CID,)).fetchone()
        return row[0] if row else CID
    def generation(self):return self.h.conn.execute('SELECT generation FROM session_coordination_meta').fetchone()[0]
    def rows(self):return self.h.conn.execute('SELECT * FROM messages ORDER BY id').fetchall()
    def count(self,table):return self.h.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_disabled_and_denied_have_no_io(self):
        def forbidden(*_):self.fail('dependency touched')
        self.rejects(lambda:self.select(w=writer(None,self.h.path,enabled=False,history=forbidden)),'lifecycle_disabled')
        self.rejects(lambda:self.select(w=writer(None,self.h.path,authorize=lambda *_:False)),'coordination_access_denied')

    def test_snapshot_has_no_config_message_schema_or_journal_writes(self):
        before=self.h.config_path.read_bytes();rows=self.rows();schema=self.h.conn.execute('SELECT * FROM sqlite_master').fetchall()
        with (patch.object(versions,'finish',side_effect=AssertionError('must not repair history')),
              patch.object(versions,'public_state',side_effect=AssertionError('must not use mutating public state'))):
            self.assertEqual(self.snapshot(),self.request)
        self.assertEqual(self.request['expected_group_ids'],sorted([CID,A,B,C]))
        self.assertEqual(self.rows(),rows);self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.h.conn.execute('SELECT * FROM sqlite_master').fetchall(),schema)
        self.assertEqual(self.count('session_coordination_ops'),0)
        for value in ('synthetic private','synthetic secret','synthetic.invalid'):self.assertNotIn(value,str(self.request))

    def test_selection_generation_and_receipt_are_atomic_config_byte_identical(self):
        before=self.h.config_path.read_bytes();rows=self.rows();g3=self.h.conn.execute('SELECT * FROM fixture_g3').fetchall()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('no JSON publication')):
            receipt=self.select()
        self.assertEqual((receipt.generation,receipt.replayed),(1,False));self.assertEqual(self.selected(),A)
        self.assertEqual(self.generation(),1);self.assertEqual(self.rows(),rows);self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertEqual(self.h.conn.execute('SELECT * FROM fixture_g3').fetchall(),g3)
        row=self.h.conn.execute('SELECT old_sha,new_sha,status FROM session_coordination_ops').fetchone()
        self.assertEqual(row[0],row[1]);self.assertEqual(row[2],'committed')
        self.assertFalse(list((self.root/'gate').glob('*.stage')))

    def test_real_select_entry_binds_lock_paths_and_avoids_public_state(self):
        kwargs={'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock,
            'request_id':'select','access':ACCESS,**self.request}
        with patch.object(versions,'public_state',side_effect=AssertionError('legacy normalization')):
            self.assertFalse(versions.select_version(self.authority,CID,{'version_id':A},**kwargs).replayed)
            self.assertTrue(versions.select_version(self.authority,CID,{'version_id':A},**kwargs).replayed)

    def test_wrong_entry_lock_resource_or_missing_dependency_refuses(self):
        base={'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock}
        for lock in (None,RLock()):
            self.rejects(lambda:versions.select_version(self.authority,CID,{'version_id':A},**{**base,'session_lock':lock}),'coordination_lock_mismatch')
        self.rejects(lambda:versions.select_version(self.authority,CID,{'version_id':A},**{**base,'relay_db':'/wrong.db'}),'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=Path('/wrong.json')
        self.rejects(lambda:versions.select_version(self.authority,CID,{'version_id':A},**base),'coordination_resource_mismatch')
        with self.assertRaises(ForkError) as caught:
            versions.select_version(self.authority,CID,{'version_id':A},relay_db=self.h.path,request_id='select')
        self.assertEqual(caught.exception.category,'version_coordination_required')

    def test_declared_canonical_database_must_match_actual_connection(self):
        self.rejects(lambda:self.select(w=writer(self.c,self.root/'wrong.db')),'coordination_resource_mismatch')

    def test_current_group_read_and_write_scopes_include_unselected_siblings(self):
        for denied in (None,CID,A,B,C):
            def history(sid):
                if sid==denied:self.fail('history for denied scope')
                return None
            w=writer(self.c,self.h.path,authorize=lambda _a,_b,target:target!=denied,history=history)
            self.rejects(lambda:self.select(w=w),'coordination_access_denied')
            self.rejects(lambda:w.snapshot_selection(CID,{'version_id':A},access=ACCESS),'coordination_access_denied')

    def test_revocation_after_sql_effect_rolls_back_choice_generation_and_receipt(self):
        state={'allow':True};w=writer(self.c,self.h.path,authorize=lambda *_:state['allow']);inspect=w._inspect
        def revoke(*args):value=inspect(*args);state['allow']=False;return value
        w._inspect=revoke;self.rejects(lambda:self.select(w=w),'coordination_access_denied')
        self.assertEqual(self.selected(),CID);self.assertEqual(self.generation(),0);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_schema_noop_self_root_selection_and_nested_selection(self):
        self.assertFalse(self.select(target=C).replayed);self.assertEqual(self.selected(),C)
        request=self.snapshot(CID);self.select(target=CID,request=request,rid='root');self.assertEqual(self.selected(),CID)
        self.assertEqual(self.generation(),2)

    def test_stale_generation_after_canonical_selection_and_old_graph_with_fresh_generation(self):
        self.select(target=B,rid='first');self.rejects(self.select,'coordination_cas_conflict')
        self.rejects(lambda:self.select(request={**self.request,'expected_generation':1}),'coordination_cas_conflict')
        self.assertEqual(self.selected(),B)

    def test_metadata_edit_requires_new_snapshot(self):
        config_commands(self.c).patch_session('rename',CID,{'title':'later'},access=ACCESS)
        self.rejects(self.select,'coordination_cas_conflict')
        self.rejects(lambda:self.select(request={**self.request,'expected_generation':1}),'coordination_cas_conflict')

    def test_selection_invalidates_old_metadata_save_even_when_json_is_identical(self):
        metadata=self.w.snapshot_sessions(access=ACCESS);before=self.h.config_path.read_bytes();self.select()
        self.assertEqual(self.h.config_path.read_bytes(),before)
        self.rejects(lambda:self.w.save_sessions('stale_metadata',metadata.as_rows(),access=ACCESS,
            expected_generation=metadata.generation,expected_snapshot_sha=metadata.snapshot_sha),
            'coordination_cas_conflict')
        self.assertEqual(self.selected(),A)

    def test_uncooperative_selection_change_is_detected_without_overwrite(self):
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,B))
        self.rejects(self.select,'coordination_cas_conflict');self.assertEqual(self.selected(),B)

    def test_edge_change_and_same_generation_group_membership_change_are_detected(self):
        self.h.conn.execute('UPDATE web_message_versions SET point_key=point_key+1 WHERE version_id=?',(A,))
        self.rejects(self.select,'coordination_cas_conflict')
        self.h.conn.execute('DELETE FROM web_message_versions WHERE version_id=?',(C,))
        self.rejects(self.select,'coordination_cas_conflict');self.assertEqual(self.selected(),CID)

    def test_cycle_missing_parent_or_malformed_mode_refuses(self):
        self.h.conn.execute('UPDATE web_message_versions SET parent_id=? WHERE version_id=?',(C,A))
        self.rejects(self.select,'coordination_selection_graph_invalid')
        self.h.conn.execute('UPDATE web_message_versions SET parent_id=? WHERE version_id=?',('missing',A))
        self.rejects(self.select,'coordination_selection_graph_invalid')
        self.h.conn.execute('UPDATE web_message_versions SET parent_id=?,mode=? WHERE version_id=?',(CID,'branch',A))
        self.rejects(self.select,'coordination_selection_graph_invalid')

    def test_unpublished_sibling_wrong_root_and_outside_target_refuse(self):
        self.rejects(lambda:self.w.snapshot_selection(A,{'version_id':C},access=ACCESS),'coordination_selection_root_required')
        self.rejects(lambda:self.snapshot(OUT),'coordination_selection_target_unavailable')
        def remove(legacy):
            config=legacy.load_config();config['sessions']=[r for r in config['sessions'] if r['id']!=B];legacy.save_config(config)
        self.c.mutate('fixture_unpublished',{},remove)
        self.rejects(self.select,'coordination_cas_conflict')
        self.rejects(self.snapshot,'coordination_selection_graph_unavailable')

    def test_invalid_stored_selection_is_not_silently_reset(self):
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,OUT))
        self.rejects(self.select,'coordination_selection_graph_invalid')

    def test_native_group_member_is_explicitly_unsupported(self):
        def native(legacy):
            config=legacy.load_config()
            for r in config['sessions']:
                if r['id']==B:r['provider']='codex'
            legacy.save_config(config)
        self.c.mutate('fixture_native',{},native);value=self.w.snapshot_sessions(access=ACCESS)
        self.rejects(lambda:self.select(request={**self.request,'expected_generation':value.generation,
            'expected_snapshot_sha':value.snapshot_sha}),'coordination_native_selection_unsupported')

    def test_group_running_unknown_jobs_and_closed_marker_refuse(self):
        api_web_generation.Store(self.h.path).initialize()
        for sid,status,unknown in [(CID,'running',0),(A,'queued',0),(B,'stopping',0),(C,'failed',1)]:
            self.h.conn.execute('DELETE FROM api_web_generations')
            self.h.conn.execute('INSERT INTO api_web_generations(message_id,session_id,status,created_at,upstream_unknown) VALUES (1,?,?,1,?)',(sid,status,unknown))
            self.rejects(self.select,'coordination_selection_job_unsettled')
        self.h.conn.execute('DELETE FROM api_web_generations')
        self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(B,))
        self.rejects(self.select,'coordination_selection_closed')

    def test_settled_jobs_are_observed_without_expiration_or_mutation(self):
        api_web_generation.Store(self.h.path).initialize()
        self.h.conn.execute("INSERT INTO api_web_generations(message_id,session_id,status,created_at) VALUES (1,?,'completed',1)",(B,))
        before=self.h.conn.execute('SELECT * FROM api_web_generations').fetchall()
        self.request=self.snapshot();self.select();self.assertEqual(self.h.conn.execute('SELECT * FROM api_web_generations').fetchall(),before)

    def test_settled_job_change_after_snapshot_requires_new_packet(self):
        api_web_generation.Store(self.h.path).initialize()
        self.h.conn.execute("INSERT INTO api_web_generations(message_id,session_id,status,created_at) VALUES (1,?,'completed',1)",(B,))
        self.request=self.snapshot();self.h.conn.execute("UPDATE api_web_generations SET error='later metadata'")
        self.rejects(self.select,'coordination_cas_conflict');self.assertEqual(self.selected(),CID)

    def test_tool_action_references_are_not_treated_as_an_idle_api_group(self):
        self.h.conn.execute('CREATE TABLE web_mcp_actions(session_id TEXT,payload TEXT)')
        self.rejects(self.select,'coordination_cas_conflict')
        self.request=self.snapshot();self.h.conn.execute('INSERT INTO web_mcp_actions VALUES (?,?)',(B,'synthetic pending tool'))
        self.rejects(self.select,'coordination_selection_tools_unsupported');self.assertEqual(self.selected(),CID)

    def test_missing_optional_selection_table_is_not_created_by_snapshot(self):
        self.h.conn.execute('DROP TABLE web_version_selections');self.request=self.snapshot()
        self.assertFalse(self.h.conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_version_selections'").fetchone())
        self.select();self.assertEqual(self.selected(),A)

    def test_view_wrong_columns_or_trigger_refuse(self):
        self.h.conn.execute('DROP TABLE web_version_selections')
        self.h.conn.execute('CREATE VIEW web_version_selections AS SELECT 1 AS root_id')
        self.rejects(self.select,'coordination_selection_schema_invalid');self.h.conn.execute('DROP VIEW web_version_selections')
        self.h.conn.execute('CREATE TABLE web_version_selections(root_id TEXT PRIMARY KEY,version_id TEXT,extra TEXT)')
        self.rejects(self.select,'coordination_selection_schema_invalid');self.h.conn.execute('DROP TABLE web_version_selections')
        self.h.conn.execute('CREATE TABLE web_version_selections(root_id TEXT PRIMARY KEY,version_id TEXT)')
        self.h.conn.execute('CREATE TRIGGER synthetic_trigger AFTER INSERT ON web_version_selections BEGIN SELECT 1; END')
        self.rejects(self.select,'coordination_selection_schema_invalid')

    def test_bad_packet_and_missing_recovery_do_not_create_operation(self):
        for request in ({**self.request,'expected_generation':True},{**self.request,'expected_graph_sha':'bad'},
                        {**self.request,'expected_group_ids':[CID,A,A]}):
            self.rejects(lambda:self.select(request=request),'coordination_selection_invalid')
        self.rejects(lambda:self.select(recover=True),'coordination_request_missing')
        self.assertEqual(self.count('session_coordination_ops'),0)

    def test_fsync_failure_before_commit_rolls_back_selection(self):
        with patch.object(self.c,'_sync_directory',side_effect=OSError('synthetic IO fault')):
            self.rejects(self.select,'coordination_unavailable')
        self.assertEqual(self.selected(),CID);self.assertEqual(self.generation(),0);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_external_config_change_during_effect_rolls_back_sql_and_preserves_external_edit(self):
        inspect=self.w._inspect
        def change(*args):
            value=inspect(*args);config=self.h.get('legacy');config['external']='synthetic';self.h.put('legacy',config);return value
        self.w._inspect=change;self.rejects(self.select,'coordination_external_change')
        self.assertEqual(self.selected(),CID);self.assertEqual(self.generation(),0)
        self.assertEqual(self.h.get('legacy')['external'],'synthetic')

    def test_historical_replay_does_not_reset_newer_selection_or_rerun_inspection(self):
        self.select();request=self.snapshot(B);self.select(target=B,request=request,rid='later')
        with patch.object(self.w,'_inspect',side_effect=AssertionError('must not run effect')):
            receipt=self.select();recovered=self.select(recover=True)
        self.assertEqual((receipt.generation,receipt.replayed),(1,True));self.assertTrue(recovered.replayed)
        self.assertEqual(self.selected(),B);self.assertEqual(self.generation(),2)

    def test_replay_binds_actor_target_graph_and_group_and_current_authorization(self):
        self.select()
        for kwargs in ({'access':ConfigCommandAccess('other')},{'target':B},
                       {'request':{**self.request,'expected_graph_sha':'0'*64}},
                       {'request':{**self.request,'expected_group_ids':sorted([CID,A,B])}}):
            self.rejects(lambda:self.select(**kwargs),'coordination_request_conflict')
        denied=writer(self.c,self.h.path,authorize=lambda _a,_b,sid:sid!=C)
        self.rejects(lambda:self.select(w=denied),'coordination_access_denied')
        self.rejects(lambda:self.select(w=denied,recover=True),'coordination_access_denied')

    def child(self,mode):
        p=subprocess.Popen([sys.executable,'-c',
            'import sys;from backend.tests.test_session_lifecycle_version_selection import worker;worker(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        p.stdin.write(json.dumps(self.request)+'\n');p.stdin.flush()
        def cleanup():
            if p.poll() is None:p.kill()
            p.communicate(timeout=5)
        self.addCleanup(cleanup);return p
    def line(self,p):
        with selectors.DefaultSelector() as s:
            s.register(p.stdout,selectors.EVENT_READ);self.assertTrue(s.select(timeout=8));return p.stdout.readline().strip()
    def crash(self,mode):
        p=self.child(mode);_,err=p.communicate(timeout=15);self.assertEqual(p.returncode,92 if mode=='crash_before' else 93,err)

    def test_process_exit_before_sql_commit_rolls_back_choice_generation_and_receipt(self):
        before=self.h.config_path.read_bytes();self.crash('crash_before')
        self.assertEqual(self.selected(),CID);self.assertEqual(self.generation(),0);self.assertEqual(self.count('session_coordination_ops'),0)
        self.assertEqual(self.h.config_path.read_bytes(),before);self.assertFalse(self.select().replayed)

    def test_process_exit_after_commit_recovers_receipt_and_empty_stage_without_rewriting_choice(self):
        self.crash('crash_after');self.assertEqual(self.selected(),A);self.assertEqual(self.generation(),1)
        stages=list((self.root/'gate').glob('*.stage'));self.assertEqual(len(stages),1)
        self.assertEqual(stages[0].read_bytes(),b'');self.assertEqual(stages[0].stat().st_mode&0o777,0o600)
        with patch.object(self.w,'_inspect',side_effect=AssertionError('no retry SQL')):
            self.assertTrue(self.select(recover=True).replayed)
        self.assertFalse(list((self.root/'gate').glob('*.stage')));self.assertEqual(self.selected(),A)

    def test_optional_table_ddl_rolls_back_with_failed_transaction(self):
        self.h.conn.execute('DROP TABLE web_version_selections');self.request=self.snapshot();self.crash('crash_before')
        self.assertFalse(self.h.conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_version_selections'").fetchone())
        self.assertEqual(self.generation(),0)

    def test_competing_processes_commit_once_and_stale_second_request_loses(self):
        first=self.child('hold');self.assertEqual(self.line(first),'ready');same=self.child('same');other=self.child('stale')
        out,err=first.communicate('\n',timeout=15);sameout,sameerr=same.communicate(timeout=15);otherout,othererr=other.communicate(timeout=15)
        self.assertEqual((first.returncode,same.returncode,other.returncode),(0,0,0),err+sameerr+othererr)
        self.assertFalse(json.loads(out)['replayed']);self.assertTrue(json.loads(sameout)['replayed'])
        self.assertEqual(otherout.strip(),'coordination_cas_conflict');self.assertEqual(self.generation(),1)
        self.assertEqual(self.count('session_coordination_ops'),1)

    def test_journal_privacy_and_nonproduction_flags(self):
        self.select();raw=str([tuple(r) for r in self.h.conn.execute('SELECT * FROM session_coordination_ops')])
        for value in ('synthetic private','synthetic secret','synthetic.invalid',STAMP):self.assertNotIn(value,raw)
        for flag in ('production_supported','http_integrated','all_writers_integrated','native_integrated',
                     'cross_store_atomic','version_creation_integrated','runtime_selection_confirmed'):
            self.assertFalse(getattr(self.w,flag))
