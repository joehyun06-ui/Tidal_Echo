"""Offline whole-group erasure: real nested versions, SQL atomicity and recovery."""
from copy import deepcopy
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

from backend import api_web_generation,deployment_config,web_message_versions as versions,web_session_context as context
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_api_group_delete import CoordinatedApiGroupDeleteCommands
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.web_session_fork import fork_conversation,ForkError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

ACCESS=ConfigCommandAccess('synthetic_owner')
STAMP='2026-11-06T00:00:00Z'
NEXT='api-synthetic-survivor'
A='api-fork-12345678123442348234123456789abc'
B='api-fork-22345678123442348234123456789abc'
BRANCH='api-fork-32345678123442348234123456789abc'


def writer(c,path,*,authorize=lambda *_:True,history=lambda _:None,enabled=True):
    return CoordinatedApiGroupDeleteCommands(c,canonical_path=path,session_lock=RLock(),authorize=authorize,
        historical_provider=history,enabled=enabled)


def worker(path,mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        request=json.loads(sys.stdin.readline());c=coordinator(path);w=writer(c,path)
        if mode=='hold':
            original=w._group_state
            def hold(*args):value=original(*args);print('ready',flush=True);sys.stdin.readline();return value
            w._group_state=hold
        if mode=='crash_transaction':
            connect=c._connect
            class Proxy:
                def __init__(self,conn):self.conn=conn
                def __getattr__(self,n):return getattr(self.conn,n)
                def execute(self,sql,*args):
                    value=self.conn.execute(sql,*args)
                    if sql.startswith('DELETE FROM messages'):os._exit(112)
                    return value
            c._connect=lambda:Proxy(connect())
        operation=lambda:w.delete_api_group(CID,request_id='other' if mode=='stale' else 'delete-group',
            access=ACCESS,deleted_at=STAMP,next_active=NEXT,**request)
        if mode in {'crash_before','crash_after'}:
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode=='crash_after':original(*args)
                os._exit(111)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):operation()
        else:
            try:receipt=operation()
            except LifecycleError as e:print(e.category,flush=True)
            else:print(json.dumps({'generation':receipt.generation,'replayed':receipt.replayed}),flush=True)


@unittest.skipUnless(os.name=='posix','POSIX private coordinator')
class ApiGroupDeleteTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close);self.h.conn.row_factory=sqlite3.Row
        cfg=self.h.get('legacy');cfg['active_session']=CID;cfg['sessions'][0]['extension']={'preserve':True}
        cfg['sessions'].append({'id':NEXT,'title':'survivor','provider':'api','extension':{'keep':True}})
        cfg['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        self.h.put('legacy',cfg)
        for mid,direction,kind in [(2,'out','reply'),(3,'in','user'),(4,'out','reply')]:
            self.h.conn.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(mid,STAMP,direction,kind,
                'synthetic private '+str(mid),json.dumps({'api_session':CID})))
        self.legacy=SimpleNamespace(LOOP_CONFIG=self.h.config_path,load_config=lambda:self.h.get('legacy'),
            save_config=lambda cfg:self.h.put('legacy',cfg),now_iso=lambda:STAMP)
        self.authority=WebSessionProviderAuthority(self.legacy)
        context.save(self.h.path,CID,{'revision':0,'config':{**context.defaults(),'tone':'synthetic private tone'}})
        for source,uuid,mode in [(CID,'12345678-1234-4234-8234-123456789abc','regenerate'),
                                 (A,'22345678-1234-4234-8234-123456789abc','edit')]:
            mid=4 if source==CID else self.h.conn.execute("SELECT id FROM messages WHERE json_extract(meta,'$.api_session')=? AND direction='in' ORDER BY id LIMIT 1",(source,)).fetchone()[0]
            body={'request_id':uuid,'message_id':mid,'mode':mode}
            versions.prepare(self.authority,source,body,relay_db=self.h.path)
            fork_conversation(self.authority,source,body,relay_db=self.h.path)
            versions.finish(self.authority,'api-fork-'+uuid.replace('-',''),relay_db=self.h.path,select=True)
        self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','synthetic nested input',?)",(STAMP,json.dumps({'api_session':B})))
        fork_conversation(self.authority,CID,{'request_id':'32345678-1234-4234-8234-123456789abc','message_id':4,'mode':'branch'},relay_db=self.h.path)
        self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','outside input',?)",(STAMP,json.dumps({'api_session':NEXT})))
        (self.root/'gate').mkdir(mode=0o700);self.c=coordinator(self.h.path);self.c.bootstrap()
        self.w=writer(self.c,self.h.path);self.request=self.snapshot()
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def snapshot(self,*,root=CID,next_active=NEXT,w=None):
        return asdict((w or self.w).snapshot_group_delete(root,next_active=next_active,deleted_at=STAMP,access=ACCESS))
    def delete(self,*,w=None,request=None,recover=False,rid='delete-group',access=ACCESS,next_active=NEXT,stamp=STAMP):
        return (w or self.w).delete_api_group(CID,request_id=rid,access=access,deleted_at=stamp,next_active=next_active,
            recover=recover,**(self.request if request is None else request))
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def config(self):return self.h.get('legacy')
    def rows(self,table='messages'):return [tuple(r) for r in self.h.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
    def count(self,table):return self.h.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def group_rows(self):
        return [dict(r) for r in self.h.conn.execute("SELECT * FROM messages WHERE json_extract(meta,'$.api_session') IN (?,?,?) ORDER BY id",(CID,A,B))]
    def initialize(self):api_web_generation.Store(self.h.path).initialize()
    def pending(self):self.crash('crash_before')

    def test_default_disabled_and_denied_do_not_touch_dependencies(self):
        self.rejects(lambda:self.delete(w=writer(None,self.h.path,enabled=False)),'lifecycle_disabled')
        self.rejects(lambda:self.delete(w=writer(None,self.h.path,authorize=lambda *_:False)),'coordination_access_denied')

    def test_snapshot_readonly_bounded_group_and_no_plaintext(self):
        cfg=self.h.config_path.read_bytes();schema=self.rows('sqlite_master');rows=self.rows()
        self.assertEqual(self.snapshot(),self.request);self.assertEqual(self.request['expected_group_ids'],sorted([CID,A,B]))
        self.assertEqual(self.h.config_path.read_bytes(),cfg);self.assertEqual(self.rows(),rows);self.assertEqual(self.rows('sqlite_master'),schema)
        self.assertEqual(self.count('session_coordination_ops'),0)
        for secret in ('synthetic private','synthetic secret','synthetic.invalid'):self.assertNotIn(secret,str(self.request))

    def test_whole_group_sql_effects_and_intent_precede_single_json_publication(self):
        old=self.config();outside=[r for r in self.rows() if r[0] not in self.request['expected_message_ids']]
        g3=self.rows('fixture_g3');publish=deployment_config.atomic_write_text
        def inspect(path,text):
            self.assertEqual(self.group_rows(),[]);self.assertEqual(self.count('web_message_versions'),0)
            self.assertEqual(self.count('web_version_selections'),0);self.assertEqual(self.count('api_web_closed_sessions'),3)
            self.assertEqual(self.h.conn.execute('SELECT status FROM session_coordination_ops').fetchone()[0],'pending')
            self.assertEqual(self.config(),old);publish(path,text)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=inspect):receipt=self.delete()
        expected=deepcopy(old);expected['sessions']=[r for r in old['sessions'] if r['id'] not in {CID,A,B}]
        expected['active_session']=NEXT;expected['deleted_sessions']=[{'id':sid,'provider':'api','deleted_at':STAMP} for sid in sorted([CID,A,B])]
        self.assertEqual(self.config(),expected);self.assertEqual((receipt.generation,receipt.replayed),(1,False))
        self.assertEqual(self.rows(),outside);self.assertEqual(self.rows('fixture_g3'),g3)
        self.assertEqual(context.get(self.h.path,BRANCH)['config']['tone'],'synthetic private tone')

    def test_real_entry_bypasses_legacy_per_member_deletion(self):
        kwargs={'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock,
            'request_id':'delete-group','access':ACCESS,'deleted_at':STAMP,'next_active':NEXT,**self.request}
        with patch('backend.web_session_delete.delete_conversation',side_effect=AssertionError('per-member delete')):
            self.assertFalse(versions.delete_versions(self.authority,CID,**kwargs).replayed)
            self.assertTrue(versions.delete_versions(self.authority,CID,**kwargs).replayed)

    def test_entry_refuses_wrong_lock_config_database_and_external_cleanup(self):
        base={'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock}
        for lock in (None,RLock()):self.rejects(lambda:versions.delete_versions(self.authority,CID,**{**base,'session_lock':lock}),'coordination_lock_mismatch')
        self.rejects(lambda:versions.delete_versions(self.authority,CID,**{**base,'relay_db':'/wrong.db'}),'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=Path('/wrong.json')
        self.rejects(lambda:versions.delete_versions(self.authority,CID,**base),'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=self.h.config_path
        for more in ({'upload_dir':self.root},{'codex_store':'native.db'},{'usage_ledger':object()}):
            self.rejects(lambda:versions.delete_versions(self.authority,CID,**{**base,**more}),'coordination_group_external_cleanup_unsupported')

    def test_candidate_parameters_never_fall_back_to_legacy(self):
        with self.assertRaises(ForkError) as caught:versions.delete_versions(self.authority,CID,relay_db=self.h.path,request_id='delete-group')
        self.assertEqual(caught.exception.category,'group_delete_coordination_required')

    def test_all_member_and_fallback_scopes_precede_history(self):
        for denied in (None,CID,A,B,NEXT):
            def history(sid):
                if sid==denied:self.fail('unauthorized history')
                return None
            w=writer(self.c,self.h.path,authorize=lambda _a,_b,sid:sid!=denied,history=history)
            self.rejects(lambda:self.delete(w=w),'coordination_access_denied')
            self.rejects(lambda:self.snapshot(w=w),'coordination_access_denied')

    def test_authorization_revoked_after_group_inspection_rolls_back_everything(self):
        state={'allow':True};w=writer(self.c,self.h.path,authorize=lambda *_:state['allow']);source=w._group_state
        def revoke(*args):result=source(*args);state['allow']=False;return result
        w._group_state=revoke;before=self.rows();edges=self.rows('web_message_versions')
        self.rejects(lambda:self.delete(w=w),'coordination_access_denied')
        self.assertEqual(self.rows(),before);self.assertEqual(self.rows('web_message_versions'),edges);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_generation_and_metadata_are_cas_bound(self):
        config_commands(self.c).patch_session('rename',NEXT,{'title':'renamed'},access=ACCESS)
        self.rejects(self.delete,'coordination_cas_conflict')
        self.rejects(lambda:self.delete(request={**self.request,'expected_generation':1}),'coordination_cas_conflict')

    def test_graph_and_complete_group_are_bound(self):
        self.rejects(lambda:self.delete(request={**self.request,'expected_group_ids':sorted([CID,A])}),'coordination_group_state_changed')
        self.h.conn.execute('UPDATE web_message_versions SET point_key=point_key+1 WHERE version_id=?',(B,))
        self.rejects(self.delete,'coordination_group_state_changed')

    def test_cross_root_parent_or_selection_reference_refuses_whole_group(self):
        self.h.conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)',(NEXT,NEXT,A,'edit',1,1,1))
        self.rejects(self.snapshot,'coordination_group_cross_reference');self.h.conn.execute('DELETE FROM web_message_versions WHERE version_id=?',(NEXT,))
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(NEXT,B))
        self.rejects(self.delete,'coordination_group_cross_reference')

    def test_alternative_cannot_be_root_and_missing_member_is_not_adopted(self):
        self.rejects(lambda:self.snapshot(root=A),'coordination_selection_root_required')
        def remove(legacy):
            cfg=legacy.load_config();cfg['sessions']=[r for r in cfg['sessions'] if r['id']!=B];legacy.save_config(cfg)
        self.c.mutate('synthetic-missing-member',{'fixture':'missing-member'},remove)
        self.rejects(self.snapshot,'coordination_selection_graph_unavailable')

    def test_active_fallback_required_and_must_be_outside_group(self):
        self.rejects(lambda:self.snapshot(next_active=None),'coordination_delete_active_required')
        self.rejects(lambda:self.snapshot(next_active=B),'coordination_group_active_invalid')

    def test_no_automatic_survivor_allocation_for_last_api_group(self):
        def remove(legacy):
            cfg=legacy.load_config();cfg['sessions']=[r for r in cfg['sessions'] if r['id'] in {CID,A,B}];legacy.save_config(cfg)
        self.c.mutate('synthetic-last-group',{'fixture':'last-group'},remove)
        with patch.object(WebSessionProviderAuthority,'new_row',side_effect=AssertionError('allocation')):
            self.rejects(lambda:self.snapshot(next_active=None),'coordination_delete_last_api_unsupported')

    def test_other_group_alternative_is_not_valid_fallback(self):
        self.h.conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)',(NEXT,BRANCH,BRANCH,'edit',1,1,1))
        self.rejects(self.snapshot,'coordination_group_active_invalid')

    def test_exact_message_redaction_and_retained_digest_required(self):
        for change in ({'expected_message_ids':[]},{'expected_retained_sha':'0'*64}):
            self.rejects(lambda:self.delete(request={**self.request,**change}),'coordination_group_state_changed')
        self.rejects(lambda:self.delete(request={**self.request,'expected_redacted_ids':[True]}),'coordination_group_invalid')

    def test_changed_message_or_late_context_requires_new_snapshot(self):
        self.h.conn.execute("UPDATE messages SET text='changed' WHERE id=2")
        self.rejects(self.delete,'coordination_group_state_changed');self.request=self.snapshot()
        self.h.conn.execute('UPDATE web_session_context SET revision=revision+1 WHERE root_id=?',(CID,))
        self.rejects(self.delete,'coordination_group_state_changed')

    def test_late_child_message_is_rechecked_inside_the_intent_transaction(self):
        source=self.w._group_config;once=[]
        def late(*args,**kwargs):
            result=source(*args,**kwargs)
            if not once:
                once.append(True)
                self.h.conn.execute("INSERT INTO messages VALUES (40,?,'in','user','late nested input',?)",(STAMP,json.dumps({'api_session':B})))
            return result
        self.w._group_config=late
        self.rejects(self.delete,'coordination_group_state_changed')
        self.assertEqual(self.count('session_coordination_ops'),0);self.assertEqual(self.count('web_message_versions'),2)
        self.assertTrue(self.h.conn.execute('SELECT 1 FROM messages WHERE id=40').fetchone())

    def test_group_aggregate_payload_limit_refuses_without_any_erasure(self):
        self.h.conn.execute('UPDATE messages SET text=? WHERE id=2',('x'*(8*1024*1024),))
        self.rejects(self.snapshot,'coordination_delete_state_too_large')
        self.assertEqual(self.count('web_message_versions'),2);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_terminal_jobs_all_group_contexts_and_fingerprints_erased_together(self):
        self.initialize()
        self.h.conn.execute("INSERT INTO api_web_generations(message_id,session_id,status,created_at) VALUES (1,?,'completed',1)",(B,))
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,1,?,?)',(B,'synthetic old child context',STAMP))
        self.h.conn.execute('CREATE TABLE web_context_observations(session_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        self.h.conn.execute('INSERT INTO web_context_observations VALUES (?,?)',(A,'synthetic observation'))
        self.request=self.snapshot();self.delete()
        self.assertEqual(self.count('api_web_generations'),0);self.assertEqual(self.count('web_context_observations'),0)
        self.assertEqual([r[0] for r in self.rows('web_session_context')],[BRANCH])
        self.assertEqual([r[0] for r in self.rows('web_session_forks')],[BRANCH])

    def test_any_member_unsettled_unknown_or_closed_refuses(self):
        self.initialize()
        for status,unknown in [('queued',0),('running',0),('stopping',0),('failed',1),('unknown',0)]:
            self.h.conn.execute('DELETE FROM api_web_generations')
            self.h.conn.execute('INSERT INTO api_web_generations(message_id,session_id,status,created_at,upstream_unknown) VALUES (1,?,?,1,?)',(B,status,unknown))
            self.rejects(self.snapshot,'coordination_selection_job_unsettled')
        self.h.conn.execute('DELETE FROM api_web_generations');self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(A,))
        self.rejects(self.snapshot,'coordination_selection_closed')

    def test_attachments_mcp_settings_lifecycle_channel_and_ledger_references_refuse(self):
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=2',(json.dumps({'api_session':CID,'attachments':[{'url':'/private.png'}]}),))
        self.rejects(self.snapshot,'coordination_delete_attachments_unsupported')
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=2',(json.dumps({'api_session':CID}),))
        for table,column in [('web_mcp_chat_settings','session_id'),('channel_conversations','api_session'),('usage_ledger','session_id')]:
            self.h.conn.execute('CREATE TABLE '+table+'('+column+' TEXT)');self.h.conn.execute('INSERT INTO '+table+' VALUES (?)',(B,))
            self.rejects(self.snapshot,'coordination_group_reference_unsupported');self.h.conn.execute('DROP TABLE '+table)
        self.h.conn.execute('INSERT INTO session_lifecycle_records VALUES (?,1,?,?)',(A,'synthetic','0'*64))
        self.rejects(self.snapshot,'coordination_group_reference_unsupported')

    def test_bad_schema_trigger_or_corrupt_message_metadata_refuses(self):
        self.h.conn.execute('CREATE TRIGGER synthetic_trigger AFTER DELETE ON web_message_versions BEGIN SELECT 1; END')
        self.rejects(self.snapshot,'coordination_delete_schema_invalid');self.h.conn.execute('DROP TRIGGER synthetic_trigger')
        self.h.conn.execute("UPDATE messages SET meta='bad json' WHERE id=2")
        self.rejects(self.snapshot,'coordination_delete_state_invalid')

    def test_unknown_incoming_metadata_fk_is_rejected_even_if_empty(self):
        self.h.conn.execute('CREATE TABLE external_refs(target TEXT REFERENCES web_message_versions(version_id) ON DELETE CASCADE)')
        self.rejects(self.snapshot,'coordination_group_aux_fk_unsupported')

    def test_explicit_and_implicit_message_fks_retain_redacted_ids_without_cascade(self):
        self.h.conn.execute('PRAGMA foreign_keys=ON')
        self.h.conn.execute('CREATE TABLE refs(mid INTEGER REFERENCES Messages ON DELETE CASCADE)')
        self.h.conn.execute('CREATE TABLE refs2(mid INTEGER REFERENCES messages(id))')
        mid=self.h.conn.execute("SELECT id FROM messages WHERE json_extract(meta,'$.api_session')=? LIMIT 1",(A,)).fetchone()[0]
        self.h.conn.execute('INSERT INTO refs VALUES (?)',(mid,));self.h.conn.execute('INSERT INTO refs2 VALUES (2)')
        self.request=self.snapshot();self.assertEqual(self.request['expected_redacted_ids'],sorted([2,mid]));self.delete()
        self.assertEqual(self.count('refs'),1);self.assertEqual(self.count('refs2'),1)
        for row in self.h.conn.execute('SELECT kind,text,meta FROM messages WHERE id IN (?,?)',(2,mid)):
            self.assertEqual((row[0],row[1],json.loads(row[2])),('deleted','',{'deleted':True,'api_session':'__deleted__'}))
        self.assertFalse(self.h.conn.execute('PRAGMA foreign_key_check').fetchall())

    def test_late_message_fk_invalidates_plan(self):
        self.h.conn.execute('CREATE TABLE refs(mid INTEGER REFERENCES messages(id))');self.h.conn.execute('INSERT INTO refs VALUES (2)')
        self.rejects(self.delete,'coordination_group_state_changed')

    def child(self,mode):
        p=subprocess.Popen([sys.executable,'-c','import sys;from backend.tests.test_session_lifecycle_api_group_delete import worker;worker(sys.argv[1],sys.argv[2])',self.h.path,mode],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        p.stdin.write(json.dumps(self.request)+'\n');p.stdin.flush()
        def cleanup():
            if p.poll() is None:p.kill()
            p.communicate(timeout=5)
        self.addCleanup(cleanup);return p
    def crash(self,mode):
        p=self.child(mode);_,err=p.communicate(timeout=15);self.assertEqual(p.returncode,112 if mode=='crash_transaction' else 111,err)
    def line(self,p):
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=8));return p.stdout.readline().strip()

    def test_process_exit_mid_sql_rolls_back_all_markers_graph_content_and_intent(self):
        rows=self.rows();edges=self.rows('web_message_versions');selection=self.rows('web_version_selections');cfg=self.h.config_path.read_bytes()
        self.crash('crash_transaction');self.assertEqual(self.rows(),rows);self.assertEqual(self.rows('web_message_versions'),edges)
        self.assertEqual(self.rows('web_version_selections'),selection);self.assertEqual(self.h.config_path.read_bytes(),cfg)
        self.assertEqual(self.count('session_coordination_ops'),0);self.assertFalse(self.delete().replayed)

    def test_pending_closed_group_blocks_real_ingress_and_recovers_without_graph_or_reerasure(self):
        cfg=self.h.config_path.read_bytes();self.pending();self.initialize();self.assertEqual(self.h.config_path.read_bytes(),cfg)
        for sid in (CID,A,B):
            with self.assertRaises(api_web_generation.ApiGenerationError):api_web_generation.Store(self.h.path).accept(sid,'synthetic',{'api_session':sid})
        with (patch.object(self.w,'_group_state',side_effect=AssertionError('graph already removed')),
              patch('backend.session_lifecycle_api_group_delete._delete_ids',side_effect=AssertionError('repeat erasure'))):
            self.assertTrue(self.delete(recover=True).replayed)
        self.assertEqual(self.config()['active_session'],NEXT)

    def test_pending_restored_graph_or_selection_refuses_without_removing_it(self):
        self.pending();self.h.conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)',(A,CID,CID,'edit',1,1,1))
        self.rejects(lambda:self.delete(recover=True),'coordination_group_effect_changed');self.assertEqual(self.count('web_message_versions'),1)
        self.h.conn.execute('DELETE FROM web_message_versions');self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(NEXT,B))
        self.rejects(lambda:self.delete(recover=True),'coordination_group_effect_changed');self.assertEqual(self.count('web_version_selections'),1)

    def test_pending_new_content_or_context_blocks_publication_without_repurging(self):
        self.pending();cfg=self.h.config_path.read_bytes()
        self.h.conn.execute("INSERT INTO messages VALUES (40,?,'in','user','new private input',?)",(STAMP,json.dumps({'api_session':B})))
        self.rejects(lambda:self.delete(recover=True),'coordination_group_effect_changed');self.assertEqual(self.h.config_path.read_bytes(),cfg)
        self.assertTrue(self.h.conn.execute('SELECT 1 FROM messages WHERE id=40').fetchone());self.h.conn.execute('DELETE FROM messages WHERE id=40')
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,1,?,?)',(A,'new context',STAMP))
        self.rejects(lambda:self.delete(recover=True),'coordination_group_effect_changed')

    def test_pending_missing_marker_or_restored_erased_message_blocks_recovery(self):
        self.pending();self.h.conn.execute('DELETE FROM api_web_closed_sessions WHERE session_id=?',(B,))
        self.rejects(lambda:self.delete(recover=True),'coordination_group_closed_conflict')
        self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(B,))
        self.h.conn.execute("INSERT INTO messages VALUES (2,?,'out','reply','restored under different ID',?)",(STAMP,json.dumps({'api_session':NEXT})))
        self.rejects(lambda:self.delete(recover=True),'coordination_group_effect_changed')

    def test_pending_changed_redacted_row_does_not_get_reerased(self):
        self.h.conn.execute('CREATE TABLE refs(mid INTEGER REFERENCES messages(id))');self.h.conn.execute('INSERT INTO refs VALUES (2)')
        self.request=self.snapshot();self.pending();self.h.conn.execute("UPDATE messages SET text='new private body' WHERE id=2")
        self.rejects(lambda:self.delete(recover=True),'coordination_group_effect_changed')
        self.assertEqual(self.h.conn.execute('SELECT text FROM messages WHERE id=2').fetchone()[0],'new private body')

    def test_published_recovery_and_historical_replay_preserve_later_bypass_content(self):
        self.crash('crash_after');self.h.conn.execute("INSERT INTO messages VALUES (40,?,'in','user','later input',?)",(STAMP,json.dumps({'api_session':B})))
        before=self.rows()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('republish')):
            self.assertTrue(self.delete(recover=True).replayed)
        self.assertTrue(self.delete().replayed);self.assertEqual(self.rows(),before);self.assertEqual(len(self.config()['deleted_sessions']),3)

    def test_pending_exact_packet_and_current_group_authorization_required(self):
        self.pending();self.rejects(lambda:self.delete(recover=True,stamp='2026-11-06T00:00:01Z'),'coordination_request_conflict')
        self.rejects(lambda:self.delete(recover=True,w=writer(self.c,self.h.path,authorize=lambda _a,_b,sid:sid!=B)),'coordination_access_denied')
        self.rejects(self.snapshot,'coordination_recovery_required');self.rejects(self.delete,'coordination_recovery_required')
        self.assertTrue(self.delete(recover=True).replayed)

    def test_committed_receipt_binds_actor_group_time_fallback_and_state(self):
        self.delete()
        for kwargs in ({'access':ConfigCommandAccess('other')},{'next_active':None},{'stamp':'2026-11-06T00:00:01Z'},
                       {'request':{**self.request,'expected_group_ids':sorted([CID,A])}},
                       {'request':{**self.request,'expected_state_sha':'0'*64}}):
            self.rejects(lambda:self.delete(**kwargs),'coordination_request_conflict')

    def test_competing_processes_one_group_effect_and_same_receipt(self):
        first=self.child('hold');self.assertEqual(self.line(first),'ready');same=self.child('same');other=self.child('stale')
        out,err=first.communicate('\n',timeout=15);out2,err2=same.communicate(timeout=15);out3,err3=other.communicate(timeout=15)
        self.assertEqual((first.returncode,same.returncode,other.returncode),(0,0,0),err+err2+err3)
        self.assertFalse(json.loads(out)['replayed']);self.assertTrue(json.loads(out2)['replayed'])
        self.assertEqual(out3.strip(),'coordination_cas_conflict');self.assertEqual(self.count('session_coordination_ops'),1)

    def test_receipt_journal_privacy_and_integration_flags_remain_off(self):
        receipt=self.delete();raw=str(self.rows('session_coordination_ops'))
        for secret in ('synthetic private','synthetic secret','synthetic.invalid',STAMP):self.assertNotIn(secret,raw)
        self.assertEqual(set(asdict(receipt)),{'generation','replayed'})
        for flag in ('production_supported','http_integrated','all_writers_integrated','native_integrated','cross_store_atomic','external_cleanup_supported','g3_delete_supported'):
            self.assertFalse(getattr(self.w,flag))
