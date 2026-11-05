"""Disposable canonical-only erasure, durable API closure and exact recovery."""
from copy import deepcopy
from dataclasses import asdict
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

from backend import api_web_generation, deployment_config, web_session_context
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_api_delete import CoordinatedApiDeleteCommands,READ,DELETE,REDACTED_META
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.web_session_delete import delete_conversation
from backend.web_session_provider_authority import WebSessionProviderAuthority,WebSessionProviderAuthorityError
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

NEXT='api-synthetic-survivor'
STAMP='2026-11-05T15:00:00Z'
ACCESS=ConfigCommandAccess('synthetic_owner')


def writer(c,path,*,authorize=lambda *_:True,history=lambda _:None,enabled=True):
    return CoordinatedApiDeleteCommands(c,canonical_path=path,session_lock=RLock(),authorize=authorize,
        historical_provider=history,enabled=enabled)


def worker(path,mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        request=json.loads(sys.stdin.readline());c=coordinator(path);w=writer(c,path)
        if mode=='hold':
            original=w._state
            def hold(*args,**kwargs):
                value=original(*args,**kwargs)
                if not kwargs.get('closed'):print('ready',flush=True);sys.stdin.readline()
                return value
            w._state=hold
        if mode=='crash_transaction':
            connect=c._connect
            class Proxy:
                def __init__(self,conn):self.conn=conn
                def __getattr__(self,n):return getattr(self.conn,n)
                def execute(self,sql,*args):
                    value=self.conn.execute(sql,*args)
                    if sql.startswith('DELETE FROM messages'):os._exit(82)
                    return value
            c._connect=lambda:Proxy(connect())
        operation=lambda:w.delete_conversation(CID,request_id='second' if mode=='stale' else 'delete',
            access=ACCESS,deleted_at=STAMP,next_active=NEXT,**request)
        if mode in {'crash_before','crash_after'}:
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode=='crash_after':original(*args)
                os._exit(81)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):operation()
        else:
            try:receipt=operation()
            except LifecycleError as e:print(e.category,flush=True)
            else:print(json.dumps({'generation':receipt.generation,'replayed':receipt.replayed}),flush=True)


@unittest.skipUnless(os.name=='posix','POSIX coordinator candidate')
class ApiDeleteTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        config=self.h.get('legacy');config['active_session']=CID
        config['sessions'][0]['extension']={'synthetic':'target'}
        config['sessions'].append({'id':NEXT,'title':'survivor','provider':'api','extension':{'preserve':True}})
        config['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        self.h.put('legacy',config)
        self.h.conn.execute("INSERT INTO messages VALUES (2,?,'out','reply','synthetic private answer',?)",(STAMP,json.dumps({'api_session':CID})))
        self.h.conn.execute("INSERT INTO messages VALUES (3,?,'in','user','other chat',?)",(STAMP,json.dumps({'api_session':NEXT})))
        (self.root/'gate').mkdir(mode=0o700);self.c=coordinator(self.h.path);self.c.bootstrap()
        self.w=writer(self.c,self.h.path);self.request=self.snapshot()
        self.legacy=SimpleNamespace(LOOP_CONFIG=self.h.config_path,load_config=lambda:self.h.get('legacy'))
        self.authority=WebSessionProviderAuthority(self.legacy)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def snapshot(self):return asdict(self.w.snapshot_delete(CID,next_active=NEXT,deleted_at=STAMP,access=ACCESS))
    def delete(self,*,w=None,request=None,recover=False,rid='delete',access=ACCESS,next_active=NEXT,stamp=STAMP):
        return (w or self.w).delete_conversation(CID,request_id=rid,access=access,deleted_at=stamp,
            next_active=next_active,recover=recover,**(self.request if request is None else request))
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def config(self):return self.h.get('legacy')
    def rows(self):return [tuple(r) for r in self.h.conn.execute('SELECT * FROM messages ORDER BY id')]
    def count(self,table):return self.h.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def schema(self):return self.h.conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
    def initialize(self):api_web_generation.Store(self.h.path).initialize()

    def test_default_disabled_and_denied_do_no_io(self):
        def forbidden(*_):self.fail('dependency touched')
        self.rejects(lambda:self.delete(w=writer(None,self.h.path,enabled=False,history=forbidden)),'lifecycle_disabled')
        self.rejects(lambda:self.delete(w=writer(None,self.h.path,authorize=lambda *_:False)),'coordination_access_denied')

    def test_query_only_snapshot_does_not_initialize_or_disclose_content(self):
        before=self.h.config_path.read_bytes();schema=self.schema();rows=self.rows()
        value=self.snapshot();self.assertEqual(value,self.request)
        self.assertEqual(value['expected_message_ids'],[1,2]);self.assertEqual(value['expected_redacted_ids'],[])
        self.assertEqual(self.schema(),schema);self.assertEqual(self.rows(),rows)
        self.assertEqual(self.h.config_path.read_bytes(),before);self.assertEqual(self.count('session_coordination_ops'),0)
        for secret in ('synthetic private','synthetic secret','synthetic.invalid'):self.assertNotIn(secret,str(value))

    def test_erasure_closure_and_intent_commit_before_json_tombstone(self):
        old=self.config();g3=self.h.conn.execute('SELECT * FROM fixture_g3').fetchall();publish=deployment_config.atomic_write_text
        def inspect(path,text):
            self.assertEqual([r[0] for r in self.rows()],[3]);self.assertEqual(self.count('api_web_closed_sessions'),1)
            self.assertEqual(self.h.conn.execute('SELECT status FROM session_coordination_ops').fetchone()[0],'pending')
            self.assertEqual(self.config(),old);publish(path,text)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=inspect):receipt=self.delete()
        expected=deepcopy(old);expected['sessions']=expected['sessions'][1:];expected['active_session']=NEXT
        expected['deleted_sessions']=[{'id':CID,'provider':'api','deleted_at':STAMP}]
        self.assertEqual(self.config(),expected);self.assertEqual((receipt.generation,receipt.replayed),(1,False))
        self.assertEqual(self.h.conn.execute('SELECT * FROM fixture_g3').fetchall(),g3)
        with self.assertRaises(WebSessionProviderAuthorityError):self.authority.provider_for_session(CID)

    def test_actual_entry_same_lock_path_and_receipt_without_old_cleanup(self):
        kwargs={'relay_db':self.h.path,'upload_dir':None,'codex_store':None,'coordinated':self.w,
            'session_lock':self.w.session_lock,'request_id':'delete','access':ACCESS,'deleted_at':STAMP,'next_active':NEXT,**self.request}
        with patch('backend.web_session_delete.purge_messages',side_effect=AssertionError('legacy purge')):
            receipt=delete_conversation(self.authority,CID,**kwargs)
            self.assertTrue(delete_conversation(self.authority,CID,**kwargs).replayed)
        self.assertFalse(receipt.replayed);self.assertFalse(hasattr(receipt,'content_deleted'))

    def test_entry_wrong_lock_path_or_external_cleanup_refuses_before_erasure(self):
        kwargs={'relay_db':self.h.path,'upload_dir':None,'codex_store':None,'coordinated':self.w,'session_lock':self.w.session_lock}
        for lock in (None,RLock()):self.rejects(lambda:delete_conversation(self.authority,CID,**{**kwargs,'session_lock':lock}),'coordination_lock_mismatch')
        self.rejects(lambda:delete_conversation(self.authority,CID,**{**kwargs,'relay_db':'/wrong.db'}),'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=Path('/wrong.json')
        self.rejects(lambda:delete_conversation(self.authority,CID,**kwargs),'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=self.h.config_path
        for more in ({'upload_dir':self.root},{'codex_store':self.root/'native.db'},{'usage_ledger':object()}):
            self.rejects(lambda:delete_conversation(self.authority,CID,**{**kwargs,**more}),'coordination_delete_external_cleanup_unsupported')
        self.assertEqual(len(self.rows()),3)

    def test_candidate_parameters_without_dependency_do_not_fall_back(self):
        with self.assertRaises(WebSessionProviderAuthorityError) as caught:
            delete_conversation(self.authority,CID,relay_db=self.h.path,upload_dir=None,codex_store=None,request_id='delete')
        self.assertEqual(caught.exception.category,'web_session_coordination_required');self.assertEqual(len(self.rows()),3)

    def test_read_and_write_scopes_precede_history_access(self):
        for denied in (None,CID,NEXT):
            w=writer(self.c,self.h.path,authorize=lambda _a,_b,target:target!=denied,
                history=lambda _:self.fail('history before scope authorization'))
            self.rejects(lambda:self.delete(w=w),'coordination_access_denied')
            self.rejects(lambda:w.snapshot_delete(CID,next_active=NEXT,deleted_at=STAMP,access=ACCESS),'coordination_access_denied')

    def test_authorization_revoked_during_effect_rolls_back_all_erasure(self):
        state={'allowed':True};w=writer(self.c,self.h.path,authorize=lambda *_:state['allowed']);original=w._state
        def revoke(*args,**kwargs):value=original(*args,**kwargs);state['allowed']=False;return value
        w._state=revoke;before=self.rows();schema=self.schema()
        self.rejects(lambda:self.delete(w=w),'coordination_access_denied')
        self.assertEqual(self.rows(),before);self.assertEqual(self.schema(),schema);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_generation_and_metadata_snapshot_conflicts(self):
        config_commands(self.c).patch_session('rename',CID,{'title':'changed'},access=ACCESS)
        self.rejects(self.delete,'coordination_cas_conflict')
        self.rejects(lambda:self.delete(request={**self.request,'expected_generation':1}),'coordination_cas_conflict')
        self.assertEqual(len(self.rows()),3)

    def test_new_or_changed_message_requires_new_preflight(self):
        self.h.conn.execute("UPDATE messages SET text='changed' WHERE id=2")
        self.rejects(self.delete,'coordination_delete_state_changed');self.request=self.snapshot()
        self.h.conn.execute("INSERT INTO messages VALUES (4,?,'in','user','new',?)",(STAMP,json.dumps({'api_session':CID})))
        self.rejects(self.delete,'coordination_delete_state_changed');self.assertEqual(len(self.rows()),4)

    def test_late_message_after_buffer_is_rechecked_inside_intent_transaction(self):
        validate=self.c.validate
        def change(config):validate(config);self.h.conn.execute("UPDATE messages SET text='late' WHERE id=2")
        self.c.validate=change;self.rejects(self.delete,'coordination_delete_state_changed');self.assertEqual(len(self.rows()),3)

    def test_active_replacement_required_and_last_api_never_creates_chat(self):
        self.rejects(lambda:self.delete(next_active=None),'coordination_delete_active_required')
        self.rejects(lambda:self.delete(next_active='missing'),'coordination_delete_active_invalid')
        def last(legacy):
            config=legacy.load_config();config['sessions']=config['sessions'][:1];legacy.save_config(config)
        self.c.mutate('fixture_last',{},last);value=self.w.snapshot_sessions(access=ACCESS)
        self.request.update(expected_generation=value.generation,expected_snapshot_sha=value.snapshot_sha)
        self.rejects(self.delete,'coordination_delete_last_api_unsupported');self.assertEqual(len(self.rows()),3)

    def test_inactive_deletion_preserves_explicit_survivor_and_other_tombstones(self):
        def select(legacy):
            config=legacy.load_config();config['active_session']=NEXT
            config['deleted_sessions']=[{'id':'api-old','provider':'api','deleted_at':STAMP}];legacy.save_config(config)
        self.c.mutate('fixture_select',{},select);self.request=self.snapshot();self.delete(next_active=None)
        self.assertEqual(self.config()['active_session'],NEXT);self.assertEqual(self.config()['deleted_sessions'][0]['id'],'api-old')

    def test_native_source_legacy_and_invalid_allocation_refuse(self):
        self.rejects(lambda:self.w.snapshot_delete('__legacy__',deleted_at=STAMP,access=ACCESS),'coordination_delete_invalid')
        self.rejects(lambda:self.delete(stamp=None),'coordination_creation_invalid')
        def native(legacy):
            config=legacy.load_config();config['sessions'][0]['provider']='codex';legacy.save_config(config)
        self.c.mutate('fixture_native',{},native);value=self.w.snapshot_sessions(access=ACCESS)
        self.request.update(expected_generation=value.generation,expected_snapshot_sha=value.snapshot_sha)
        self.rejects(self.delete,'coordination_native_delete_unsupported')

    def test_running_or_uncertain_jobs_refuse_without_closing(self):
        self.initialize()
        for status,unknown in [('queued',0),('running',0),('stopping',0),('failed',1),('unknown',0)]:
            self.h.conn.execute('DELETE FROM api_web_generations')
            self.h.conn.execute('INSERT INTO api_web_generations(message_id,session_id,status,created_at,upstream_unknown) VALUES (1,?,?,1,?)',(CID,status,unknown))
            self.rejects(self.delete,'coordination_delete_job_unsettled')
        self.assertEqual(self.count('api_web_closed_sessions'),0);self.assertEqual(len(self.rows()),3)

    def test_terminal_jobs_context_observations_and_fork_receipt_erased_together(self):
        self.initialize();self.h.conn.execute("INSERT INTO api_web_generations(message_id,session_id,status,created_at) VALUES (1,?,'completed',1)",(CID,))
        web_session_context.get(self.h.path,CID)
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,1,?,?)',(CID,'synthetic private setting',STAMP))
        self.h.conn.execute('CREATE TABLE web_context_observations(session_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        self.h.conn.execute('INSERT INTO web_context_observations VALUES (?,?)',(CID,'synthetic private observation'))
        self.h.conn.execute('CREATE TABLE web_session_forks(target TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)')
        self.h.conn.execute('INSERT INTO web_session_forks VALUES (?,?)',(CID,'synthetic hash'))
        self.request=self.snapshot();self.delete()
        for table in ('api_web_generations','web_session_context','web_context_observations','web_session_forks'):self.assertEqual(self.count(table),0)

    def test_foreign_key_references_are_redacted_and_integrity_preserved(self):
        self.h.conn.execute('PRAGMA foreign_keys=ON')
        self.h.conn.execute('CREATE TABLE synthetic_refs(mid INTEGER REFERENCES messages(id) ON DELETE CASCADE)')
        self.h.conn.execute('INSERT INTO synthetic_refs VALUES (2)');self.request=self.snapshot()
        self.assertEqual(self.request['expected_redacted_ids'],[2]);self.delete()
        row=self.rows()[0];self.assertEqual((row[0],row[3],row[4],row[5]),(2,'deleted','',REDACTED_META))
        self.assertEqual(self.count('synthetic_refs'),1);self.assertFalse(self.h.conn.execute('PRAGMA foreign_key_check').fetchall())

    def test_new_foreign_key_reference_invalidates_deletion_plan(self):
        self.h.conn.execute('CREATE TABLE synthetic_refs(mid INTEGER REFERENCES messages(id))')
        self.h.conn.execute('INSERT INTO synthetic_refs VALUES (2)')
        self.rejects(self.delete,'coordination_delete_state_changed');self.assertEqual(len(self.rows()),3)

    def test_implicit_primary_key_and_case_insensitive_fk_avoid_unintended_cascade(self):
        self.h.conn.execute('PRAGMA foreign_keys=ON')
        self.h.conn.execute('CREATE TABLE synthetic_refs(mid INTEGER REFERENCES Messages ON DELETE CASCADE)')
        self.h.conn.execute('INSERT INTO synthetic_refs VALUES (2)');self.request=self.snapshot()
        self.assertEqual(self.request['expected_redacted_ids'],[2]);self.delete()
        self.assertEqual(self.count('synthetic_refs'),1);self.assertEqual(self.rows()[0][3],'deleted')

    def test_context_change_and_byte_limit_invalidate_preflight(self):
        web_session_context.get(self.h.path,CID)
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,1,?,?)',(CID,'synthetic context',STAMP))
        self.rejects(self.delete,'coordination_delete_state_changed')
        self.h.conn.execute('DELETE FROM web_session_context')
        self.h.conn.execute('UPDATE messages SET text=? WHERE id=2',('x'*(8*1024*1024),))
        self.rejects(self.delete,'coordination_delete_state_too_large');self.assertEqual(len(self.rows()),3)

    def test_already_closed_target_is_not_silently_treated_as_completed_delete(self):
        self.initialize();self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(CID,))
        self.rejects(self.delete,'coordination_delete_closed_conflict');self.assertEqual(len(self.rows()),3)

    def test_attachments_mcp_versions_lifecycle_and_ledger_references_refuse(self):
        original=self.rows()[1][-1]
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=2',(json.dumps({'api_session':CID,'attachments':[{'url':'/uploads/private.png'}]}),))
        self.rejects(self.delete,'coordination_delete_attachments_unsupported')
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=2',(original,))
        for table,column in [('web_mcp_actions','session_id'),('usage_ledger','session_id')]:
            self.h.conn.execute('CREATE TABLE '+table+'('+column+' TEXT)');self.h.conn.execute('INSERT INTO '+table+' VALUES (?)',(CID,))
            self.rejects(self.delete,'coordination_delete_reference_unsupported');self.h.conn.execute('DROP TABLE '+table)
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,CID))
        self.rejects(self.delete,'coordination_delete_reference_unsupported');self.h.conn.execute('DELETE FROM web_version_selections')
        self.h.conn.execute('INSERT INTO session_lifecycle_records VALUES (?,1,?,?)',(CID,'synthetic','0'*64))
        self.rejects(self.delete,'coordination_delete_reference_unsupported');self.assertEqual(len(self.rows()),3)

    def test_bad_schema_triggers_and_corrupt_metadata_fail_closed(self):
        self.h.conn.execute('CREATE TRIGGER synthetic_trigger AFTER DELETE ON messages BEGIN SELECT 1; END')
        self.rejects(self.delete,'coordination_delete_schema_invalid');self.h.conn.execute('DROP TRIGGER synthetic_trigger')
        self.h.conn.execute('CREATE VIEW api_web_closed_sessions AS SELECT 1 AS session_id')
        self.rejects(self.delete,'coordination_delete_schema_invalid');self.h.conn.execute('DROP VIEW api_web_closed_sessions')
        self.h.conn.execute("UPDATE messages SET meta='bad json' WHERE id=2")
        self.rejects(self.delete,'coordination_delete_state_invalid')

    def test_exact_message_and_redaction_packet_is_required(self):
        for request in ({**self.request,'expected_message_ids':[1]}, {**self.request,'expected_retained_sha':'0'*64}):
            self.rejects(lambda:self.delete(request=request),'coordination_delete_state_changed')
        self.rejects(lambda:self.delete(request={**self.request,'expected_redacted_ids':[True]}),'coordination_delete_invalid')

    def child(self,mode):
        p=subprocess.Popen([sys.executable,'-c',
            'import sys;from backend.tests.test_session_lifecycle_api_delete import worker;worker(sys.argv[1],sys.argv[2])',
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
        p=self.child(mode);_,error=p.communicate(timeout=15);self.assertEqual(p.returncode,82 if mode=='crash_transaction' else 81,error)

    def test_process_exit_during_erasure_rolls_back_rows_marker_and_intent(self):
        before=self.rows();schema=self.schema();config=self.h.config_path.read_bytes();self.crash('crash_transaction')
        self.assertEqual(self.rows(),before);self.assertEqual(self.schema(),schema)
        self.assertEqual(self.h.config_path.read_bytes(),config);self.assertEqual(self.count('session_coordination_ops'),0)
        self.assertFalse(self.delete().replayed)

    def test_before_publication_closure_blocks_real_accept_and_recovery_does_not_reerase(self):
        old=self.h.config_path.read_bytes();self.crash('crash_before');self.initialize()
        with self.assertRaises(api_web_generation.ApiGenerationError):api_web_generation.Store(self.h.path).accept(CID,'synthetic',{'api_session':CID})
        self.assertEqual([r[0] for r in self.rows()],[3]);self.assertEqual(self.h.config_path.read_bytes(),old)
        with patch('backend.session_lifecycle_api_delete._delete_ids',side_effect=AssertionError('must not erase again')):
            self.assertTrue(self.delete(recover=True).replayed)
        self.assertEqual(self.config()['active_session'],NEXT)

    def test_pending_bypass_ingress_refuses_publication_without_repurging_new_data(self):
        self.crash('crash_before');old=self.h.config_path.read_bytes()
        self.h.conn.execute("INSERT INTO messages VALUES (20,?,'in','user','unreviewed later input',?)",(STAMP,json.dumps({'api_session':CID})))
        self.rejects(lambda:self.delete(recover=True),'coordination_delete_effect_changed')
        self.assertEqual(self.h.config_path.read_bytes(),old);self.assertEqual([r[0] for r in self.rows()],[3,20])

    def test_missing_close_marker_and_restored_erased_message_block_recovery(self):
        self.crash('crash_before');self.h.conn.execute('DELETE FROM api_web_closed_sessions')
        self.rejects(lambda:self.delete(recover=True),'coordination_delete_closed_conflict')
        self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(CID,))
        self.h.conn.execute("INSERT INTO messages VALUES (1,?,'in','user','restored foreign message',?)",(STAMP,json.dumps({'api_session':NEXT})))
        self.rejects(lambda:self.delete(recover=True),'coordination_delete_effect_changed')

    def test_changed_redacted_fk_row_blocks_pending_publication_without_redeleting(self):
        self.h.conn.execute('CREATE TABLE synthetic_refs(mid INTEGER REFERENCES messages(id))')
        self.h.conn.execute('INSERT INTO synthetic_refs VALUES (2)');self.request=self.snapshot();self.crash('crash_before')
        self.h.conn.execute("UPDATE messages SET text='restored unreviewed body' WHERE id=2")
        self.rejects(lambda:self.delete(recover=True),'coordination_delete_effect_changed')
        self.assertEqual(self.rows()[0][4],'restored unreviewed body');self.assertEqual(self.count('synthetic_refs'),1)

    def test_after_publication_and_committed_replay_are_historical_no_blanket_erasure(self):
        self.crash('crash_after')
        self.h.conn.execute("INSERT INTO messages VALUES (20,?,'in','user','bypassed later input',?)",(STAMP,json.dumps({'api_session':CID})))
        before=self.rows()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('no republish')):
            self.assertTrue(self.delete(recover=True).replayed)
        self.assertTrue(self.delete().replayed);self.assertEqual(self.rows(),before)
        self.assertEqual(len(self.config()['deleted_sessions']),1)

    def test_pending_requires_same_request_packet_and_current_authorization(self):
        self.crash('crash_before')
        self.rejects(lambda:self.delete(recover=True,stamp='2026-11-05T15:00:01Z'),'coordination_request_conflict')
        self.rejects(lambda:self.delete(recover=True,w=writer(self.c,self.h.path,authorize=lambda *_:False)),'coordination_access_denied')
        self.rejects(self.snapshot,'coordination_recovery_required');self.rejects(self.delete,'coordination_recovery_required')
        self.assertTrue(self.delete(recover=True).replayed)

    def test_committed_id_binds_actor_time_active_and_digest(self):
        self.delete()
        for kwargs in ({'access':ConfigCommandAccess('other')},{'next_active':None},{'stamp':'2026-11-05T15:00:01Z'},
                       {'request':{**self.request,'expected_state_sha':'0'*64}}):
            self.rejects(lambda:self.delete(**kwargs),'coordination_request_conflict')

    def test_competing_processes_replay_one_erasure_and_stale_other_request_loses(self):
        first=self.child('hold');self.assertEqual(self.line(first),'ready');same=self.child('same');other=self.child('stale')
        out,err=first.communicate('\n',timeout=15);sameout,sameerr=same.communicate(timeout=15);otherout,othererr=other.communicate(timeout=15)
        self.assertEqual((first.returncode,same.returncode,other.returncode),(0,0,0),err+sameerr+othererr)
        self.assertFalse(json.loads(out)['replayed']);self.assertTrue(json.loads(sameout)['replayed'])
        self.assertEqual(otherout.strip(),'coordination_cas_conflict');self.assertEqual([r[0] for r in self.rows()],[3])
        self.assertEqual(self.count('session_coordination_ops'),1)

    def test_receipts_and_journal_do_not_claim_global_or_current_deletion(self):
        receipt=self.delete();raw=str([tuple(r) for r in self.h.conn.execute('SELECT * FROM session_coordination_ops')])
        for private in ('synthetic private','synthetic secret','synthetic.invalid',STAMP):self.assertNotIn(private,raw)
        self.assertEqual(set(asdict(receipt)),{'generation','replayed'})
        for flag in ('production_supported','http_integrated','all_writers_integrated','native_integrated',
                     'cross_store_atomic','external_cleanup_supported','g3_delete_supported'):self.assertFalse(getattr(self.w,flag))
