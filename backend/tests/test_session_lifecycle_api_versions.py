"""Synthetic composite versions, exact-prefix effects and crash recovery."""
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
from backend.session_lifecycle_api_versions import CoordinatedApiVersionCommands,READ,CREATE
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.web_session_fork import fork_conversation,ForkError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

ACCESS=ConfigCommandAccess('synthetic_owner')
STAMP='2026-11-06T00:00:00Z'
TITLE='synthetic version'
OLD_UUID='12345678-1234-4234-8234-123456789abc'
A='api-fork-'+OLD_UUID.replace('-','')
NEW_UUID='42345678-1234-4234-8234-123456789abc'
TARGET='api-fork-'+NEW_UUID.replace('-','')
BODY={'request_id':NEW_UUID,'message_id':4,'mode':'regenerate'}
OUT='api-synthetic-unrelated'


def writer(c,path,*,authorize=lambda *_:True,history=lambda _:None,enabled=True):
    return CoordinatedApiVersionCommands(c,canonical_path=path,session_lock=RLock(),authorize=authorize,
        historical_provider=history,enabled=enabled)


def worker(path,mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        value=json.loads(sys.stdin.readline());c=coordinator(path);w=writer(c,path);body=value['body']
        if mode=='stale':body={**body,'request_id':'52345678-1234-4234-8234-123456789abc'}
        if mode=='hold':
            original=w._version_state
            def hold(*args):result=original(*args);print('ready',flush=True);sys.stdin.readline();return result
            w._version_state=hold
        if mode=='crash_transaction':
            connect=c._connect
            class Proxy:
                def __init__(self,conn):self.conn=conn
                def __getattr__(self,n):return getattr(self.conn,n)
                def execute(self,sql,*args):
                    result=self.conn.execute(sql,*args)
                    if sql.startswith('INSERT INTO messages'):os._exit(102)
                    return result
            c._connect=lambda:Proxy(connect())
        operation=lambda:w.create_api_version(CID,value['source'],body,title=TITLE,created_at=STAMP,
            select=value['select'],access=ACCESS,**value['request'])
        if mode in {'crash_before','crash_after'}:
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode=='crash_after':original(*args)
                os._exit(101)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):operation()
        else:
            try:receipt=operation()
            except LifecycleError as e:print(e.category,flush=True)
            else:print(json.dumps({'generation':receipt.generation,'replayed':receipt.replayed}),flush=True)


@unittest.skipUnless(os.name=='posix','POSIX private coordinator')
class ApiVersionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        self.h.conn.row_factory=sqlite3.Row
        config=self.h.get('legacy');config['active_session']=CID;config['sessions'][0]['extension']={'preserve':True}
        config['sessions'].append({'id':OUT,'title':'unrelated','provider':'api'})
        config['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        self.h.put('legacy',config)
        for mid,direction,kind in [(2,'out','reply'),(3,'in','user'),(4,'out','reply'),(5,'in','user'),(6,'out','reply')]:
            self.h.conn.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(mid,STAMP,direction,kind,
                'synthetic private text '+str(mid),json.dumps({'api_session':CID,'generation_id':'old','api':{'usage':{'total_tokens':10}}})))
        self.legacy=SimpleNamespace(LOOP_CONFIG=self.h.config_path,load_config=lambda:self.h.get('legacy'),
            save_config=lambda cfg:self.h.put('legacy',cfg),now_iso=lambda:STAMP)
        self.authority=WebSessionProviderAuthority(self.legacy)
        context.save(self.h.path,CID,{'revision':0,'config':{**context.defaults(),'tone':'synthetic shared tone'}})
        old={'request_id':OLD_UUID,'message_id':4,'mode':'regenerate'}
        versions.prepare(self.authority,CID,old,relay_db=self.h.path)
        fork_conversation(self.authority,CID,old,relay_db=self.h.path)
        versions.finish(self.authority,A,relay_db=self.h.path)
        self.alt_in=self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','alternative prompt',?)",(STAMP,json.dumps({'api_session':A}))).lastrowid
        self.alt_out=self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'out','reply','alternative answer',?)",(STAMP,json.dumps({'api_session':A}))).lastrowid
        (self.root/'gate').mkdir(mode=0o700);self.c=coordinator(self.h.path);self.c.bootstrap()
        self.w=writer(self.c,self.h.path);self.request=self.snapshot()
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def snapshot(self,*,source=CID,body=None,select=True):
        return asdict(self.w.snapshot_version(CID,source,BODY if body is None else body,title=TITLE,created_at=STAMP,select=select,access=ACCESS))
    def create(self,*,source=CID,body=None,request=None,select=True,w=None,access=ACCESS,title=TITLE,stamp=STAMP,recover=False):
        return (w or self.w).create_api_version(CID,source,BODY if body is None else body,title=title,
            created_at=stamp,select=select,access=access,recover=recover,**(self.request if request is None else request))
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def rows(self,sid=TARGET):
        return [dict(r) for r in self.h.conn.execute("SELECT * FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id",(sid,))]
    def config(self):return self.h.get('legacy')
    def edge(self):
        r=self.h.conn.execute('SELECT * FROM web_message_versions WHERE version_id=?',(TARGET,)).fetchone()
        return dict(r) if r else None
    def selected(self):
        r=self.h.conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?',(CID,)).fetchone()
        return r[0] if r else None
    def generation(self):return self.h.conn.execute('SELECT generation FROM session_coordination_meta').fetchone()[0]
    def count(self,table):return self.h.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_disabled_and_denied_do_no_io(self):
        def forbidden(*_):self.fail('dependency touched')
        self.rejects(lambda:self.create(w=writer(None,self.h.path,enabled=False,history=forbidden)),'lifecycle_disabled')
        self.rejects(lambda:self.create(w=writer(None,self.h.path,authorize=lambda *_:False)),'coordination_access_denied')

    def test_snapshot_is_read_only_no_legacy_prepare_finish_or_allocation(self):
        raw=self.h.config_path.read_bytes();rows=self.h.conn.execute('SELECT * FROM messages').fetchall();schema=self.h.conn.execute('SELECT * FROM sqlite_master').fetchall()
        with (patch.object(versions,'prepare',side_effect=AssertionError('partial reserve')),
              patch.object(versions,'finish',side_effect=AssertionError('late annotation')),
              patch.object(WebSessionProviderAuthority,'new_row',side_effect=AssertionError('allocation'))):
            self.assertEqual(self.snapshot(),self.request)
        self.assertEqual(self.h.config_path.read_bytes(),raw);self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(),rows)
        self.assertEqual(self.h.conn.execute('SELECT * FROM sqlite_master').fetchall(),schema);self.assertEqual(self.count('session_coordination_ops'),0)
        self.assertEqual(self.request['expected_group_ids'],sorted([CID,A]))
        for private in ('synthetic private','synthetic secret','synthetic shared tone'):self.assertNotIn(private,str(self.request))

    def test_copy_annotations_edge_selection_and_intent_commit_before_json(self):
        old=self.config();source=self.rows(CID);g3=self.h.conn.execute('SELECT * FROM fixture_g3').fetchall();publish=deployment_config.atomic_write_text
        def inspect(path,text):
            self.assertEqual(len(self.rows()),2);self.assertEqual(self.edge()['root_id'],CID);self.assertEqual(self.selected(),TARGET)
            self.assertEqual(self.h.conn.execute('SELECT status FROM session_coordination_ops').fetchone()[0],'pending')
            self.assertEqual(self.config(),old);publish(path,text)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=inspect):receipt=self.create()
        self.assertEqual((receipt.generation,receipt.replayed),(1,False));self.assertEqual(self.rows(CID),source)
        self.assertEqual(self.config()['active_session'],CID);self.assertEqual(self.config()['sessions'][:-1],old['sessions'])
        self.assertEqual(self.config()['main_chain'],old['main_chain']);self.assertEqual(self.h.conn.execute('SELECT * FROM fixture_g3').fetchall(),g3)
        self.assertEqual((self.edge()['point_key'],self.edge()['prompt_key']),(4,3))
        for index,message in enumerate(self.rows(),1):
            meta=json.loads(message['meta']);self.assertTrue(meta['version_copy']);self.assertEqual(meta['version_key'],index)
            self.assertEqual(meta['branch_origin'],{'session_id':CID,'message_id':index});self.assertNotIn('generation_id',meta);self.assertNotIn('api',meta)
        self.assertEqual(json.loads(self.rows()[1]['meta'])['version_usage'],{'total_tokens':10})

    def test_actual_composite_entry_avoids_legacy_partial_functions_and_replays(self):
        kwargs={'root_id':CID,'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock,
            'title':TITLE,'created_at':STAMP,'access':ACCESS,**self.request}
        with (patch.object(versions,'prepare',side_effect=AssertionError('reserve')),
              patch.object(versions,'finish',side_effect=AssertionError('finish')),
              patch.object(WebSessionProviderAuthority,'new_row',side_effect=AssertionError('new ID/time'))):
            receipt=versions.create_api_version(self.authority,CID,BODY,**kwargs)
            self.assertTrue(versions.create_api_version(self.authority,CID,BODY,**kwargs).replayed)
        self.assertFalse(receipt.replayed);self.assertEqual(len(self.rows()),2)

    def test_entry_requires_dependency_same_lock_and_paths(self):
        base={'root_id':CID,'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock}
        with self.assertRaises(ForkError) as caught:versions.create_api_version(self.authority,CID,BODY,root_id=CID,relay_db=self.h.path)
        self.assertEqual(caught.exception.category,'version_creation_coordination_required')
        for lock in (None,RLock()):self.rejects(lambda:versions.create_api_version(self.authority,CID,BODY,**{**base,'session_lock':lock}),'coordination_lock_mismatch')
        self.rejects(lambda:versions.create_api_version(self.authority,CID,BODY,**{**base,'relay_db':'/wrong.db'}),'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=Path('/wrong.json');self.rejects(lambda:versions.create_api_version(self.authority,CID,BODY,**base),'coordination_resource_mismatch')

    def test_declared_database_must_match_actual_connection(self):
        self.rejects(lambda:self.create(w=writer(self.c,self.root/'wrong.db')),'coordination_resource_mismatch')

    def test_global_group_and_new_target_scopes_precede_history(self):
        for denied in (None,CID,A,TARGET):
            def history(sid):
                if sid==denied:self.fail('history for denied ID')
                return None
            w=writer(self.c,self.h.path,authorize=lambda _a,_b,target:target!=denied,history=history)
            self.rejects(lambda:self.create(w=w),'coordination_access_denied')
            self.rejects(lambda:w.snapshot_version(CID,CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS),'coordination_access_denied')

    def test_authorization_revoked_during_copy_rolls_back_effects(self):
        state={'allow':True};w=writer(self.c,self.h.path,authorize=lambda *_:state['allow']);source=w._version_state
        def revoke(*args):value=source(*args);state['allow']=False;return value
        w._version_state=revoke;self.rejects(lambda:self.create(w=w),'coordination_access_denied')
        self.assertIsNone(self.edge());self.assertEqual(self.rows(),[]);self.assertIsNone(self.selected());self.assertEqual(self.count('session_coordination_ops'),0)

    def test_edit_and_first_prompt_empty_copy_do_not_create_generated_messages(self):
        body={**BODY,'mode':'edit','message_id':1};self.request=self.snapshot(body=body);before=self.count('messages')
        self.create(body=body);self.assertEqual(self.rows(),[]);self.assertEqual(self.count('messages'),before)
        self.assertEqual((self.edge()['point_key'],self.edge()['prompt_key']),(1,1));self.assertEqual(self.selected(),TARGET)

    def test_select_false_preserves_existing_selection_and_context_is_shared(self):
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,A));self.request=self.snapshot(select=False)
        self.create(select=False);self.assertEqual(self.selected(),A)
        self.assertFalse(self.h.conn.execute('SELECT 1 FROM web_session_context WHERE root_id=?',(TARGET,)).fetchone())
        self.assertEqual(context.get(self.h.path,TARGET)['root_id'],CID)
        self.assertEqual(context.get(self.h.path,TARGET)['config']['tone'],'synthetic shared tone')

    def test_select_false_preserves_absent_selection_without_initializing_table(self):
        self.h.conn.execute('DROP TABLE web_version_selections');self.request=self.snapshot(select=False)
        self.create(select=False)
        self.assertFalse(self.h.conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_version_selections'").fetchone())

    def test_nested_regenerate_and_edit_use_existing_stable_keys(self):
        for mode,mid in [('regenerate',self.alt_out),('edit',self.alt_in)]:
            body={**BODY,'mode':mode,'message_id':mid}
            if mode=='edit':body['request_id']='62345678-1234-4234-8234-123456789abc'
            request=self.snapshot(source=A,body=body);self.create(source=A,body=body,request=request)
            target='api-fork-'+body['request_id'].replace('-','')
            edge=dict(self.h.conn.execute('SELECT * FROM web_message_versions WHERE version_id=?',(target,)).fetchone())
            self.assertEqual((edge['root_id'],edge['parent_id']),(CID,A))
            self.assertEqual((edge['point_key'],edge['prompt_key']),(4 if mode=='regenerate' else 3,3))
            self.assertEqual([json.loads(r['meta'])['version_key'] for r in self.rows(target)],[1,2])

    def test_later_turn_on_existing_alternative_gets_its_own_point(self):
        new_in=self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','later turn',?)",(STAMP,json.dumps({'api_session':A}))).lastrowid
        new_out=self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'out','reply','later answer',?)",(STAMP,json.dumps({'api_session':A}))).lastrowid
        body={**BODY,'message_id':new_out};request=self.snapshot(source=A,body=body)
        self.create(source=A,body=body,request=request)
        self.assertEqual((self.edge()['point_key'],self.edge()['prompt_key']),(new_out,new_in))
        self.assertEqual([json.loads(r['meta'])['version_key'] for r in self.rows()],[1,2,3,4])

    def test_prompt_attachment_refuses_but_earlier_attachment_reference_is_retained(self):
        original=self.rows(CID)[2]['meta']
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=3',(json.dumps({'api_session':CID,'attachments':[{'url':'/uploads/synthetic.png'}]}),))
        self.rejects(self.create,'coordination_version_prompt_attachment_unsupported')
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=3',(original,))
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=1',(json.dumps({'api_session':CID,'attachments':[{'url':'/uploads/synthetic.png'}]}),))
        self.request=self.snapshot();self.create();self.assertEqual(json.loads(self.rows()[0]['meta'])['attachments'],[{'url':'/uploads/synthetic.png'}])

    def test_stale_generation_or_metadata_and_graph_refuse(self):
        config_commands(self.c).patch_session('rename',CID,{'title':'changed'},access=ACCESS)
        self.rejects(self.create,'coordination_cas_conflict')
        self.rejects(lambda:self.create(request={**self.request,'expected_generation':1}),'coordination_cas_conflict')

    def test_source_context_and_even_post_point_history_changes_invalidate_packet(self):
        self.h.conn.execute("UPDATE messages SET text='late source change' WHERE id=6")
        self.rejects(self.create,'coordination_version_source_changed');self.request=self.snapshot()
        value=context.get(self.h.path,CID);context.save(self.h.path,CID,{'revision':value['revision'],'config':{**value['config'],'tone':'changed'}})
        self.rejects(self.create,'coordination_version_source_changed');self.assertIsNone(self.edge())

    def test_group_selection_change_and_copy_digest_change_are_bound(self):
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,A))
        self.rejects(self.create,'coordination_version_source_changed');self.request=self.snapshot()
        self.rejects(lambda:self.create(request={**self.request,'expected_copy_sha':'0'*64}),'coordination_version_source_changed')

    def test_late_source_change_before_intent_is_checked_in_sql_transaction(self):
        validate=self.c.validate
        def change(config):validate(config);self.h.conn.execute("UPDATE messages SET text='late' WHERE id=2")
        self.c.validate=change;self.rejects(self.create,'coordination_version_source_changed');self.assertEqual(self.rows(),[])

    def test_foreign_roles_bad_mode_packet_and_allocation_refuse(self):
        for body in ({**BODY,'message_id':3},{**BODY,'message_id':999}):self.rejects(lambda:self.create(body=body),'coordination_version_message_role_invalid')
        for body in ({**BODY,'mode':'branch'},{**BODY,'message_id':True},{**BODY,'message_id':2**63},{**BODY,'request_id':'bad'}):
            self.rejects(lambda:self.create(body=body),'coordination_version_invalid')
        self.rejects(lambda:self.create(stamp=None),'coordination_creation_invalid')

    def test_reserved_or_orphan_target_is_never_adopted(self):
        self.h.conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)',(TARGET,CID,CID,'edit',3,3,3))
        self.rejects(self.create,'coordination_session_namespace_conflict');self.h.conn.execute('DELETE FROM web_message_versions WHERE version_id=?',(TARGET,))
        self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','orphan',?)",(STAMP,json.dumps({'api_session':TARGET})))
        self.rejects(self.create,'coordination_session_namespace_conflict')

    def test_closed_unsettled_jobs_and_tool_refs_refuse(self):
        api_web_generation.Store(self.h.path).initialize()
        self.h.conn.execute("INSERT INTO api_web_generations(message_id,session_id,status,created_at) VALUES (1,?,'running',1)",(A,))
        self.rejects(self.create,'coordination_selection_job_unsettled');self.h.conn.execute('DELETE FROM api_web_generations')
        self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(CID,))
        self.rejects(self.create,'coordination_selection_closed');self.h.conn.execute('DELETE FROM api_web_closed_sessions')
        self.h.conn.execute('CREATE TABLE web_mcp_actions(session_id TEXT)');self.h.conn.execute('INSERT INTO web_mcp_actions VALUES (?)',(CID,))
        self.rejects(self.create,'coordination_selection_tools_unsupported')

    def test_orphan_tool_namespace_for_fresh_target_is_not_adopted(self):
        self.h.conn.execute('CREATE TABLE web_mcp_actions(session_id TEXT)')
        self.h.conn.execute('INSERT INTO web_mcp_actions VALUES (?)',(TARGET,))
        self.rejects(self.create,'coordination_version_namespace_conflict')
        self.assertIsNone(self.edge());self.assertEqual(self.count('session_coordination_ops'),0)

    def test_native_group_and_foreign_source_refuse_without_native_calls(self):
        self.rejects(lambda:self.snapshot(source=OUT),'coordination_selection_target_unavailable')
        def native(legacy):
            cfg=legacy.load_config()
            for r in cfg['sessions']:
                if r['id']==A:r['provider']='codex'
            legacy.save_config(cfg)
        self.c.mutate('fixture_native',{},native);value=self.w.snapshot_sessions(access=ACCESS)
        self.rejects(lambda:self.create(request={**self.request,'expected_generation':value.generation,'expected_snapshot_sha':value.snapshot_sha}),
            'coordination_native_selection_unsupported')

    def test_missing_copy_key_corrupt_context_and_history_limits_refuse(self):
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=?',(json.dumps({'api_session':A,'version_copy':True}),self.rows(A)[0]['id']))
        body={**BODY,'message_id':self.alt_out}
        self.rejects(lambda:self.create(source=A,body=body),'coordination_version_history_invalid')
        self.h.conn.execute("UPDATE web_session_context SET config='bad json' WHERE root_id=?",(CID,))
        self.rejects(self.create,'coordination_version_context_invalid')
        self.h.conn.execute('DELETE FROM web_session_context')
        self.h.conn.execute('UPDATE messages SET text=? WHERE id=2',('x'*(8*1024*1024),))
        self.rejects(self.create,'coordination_version_history_too_large')

    def test_new_version_is_accepted_by_existing_selection_candidate_without_finish(self):
        self.create();packet=asdict(self.w.snapshot_selection(CID,{'version_id':TARGET},access=ACCESS))
        with patch.object(versions,'finish',side_effect=AssertionError('annotations already complete')):
            self.w.select_version(CID,{'version_id':TARGET},request_id='select_created',access=ACCESS,**packet)
        self.assertEqual(self.selected(),TARGET);self.assertEqual(self.generation(),2)

    def child(self,mode,*,source=CID,body=None,select=True):
        p=subprocess.Popen([sys.executable,'-c',
            'import sys;from backend.tests.test_session_lifecycle_api_versions import worker;worker(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        p.stdin.write(json.dumps({'request':self.request,'source':source,'body':BODY if body is None else body,'select':select})+'\n');p.stdin.flush()
        def cleanup():
            if p.poll() is None:p.kill()
            p.communicate(timeout=5)
        self.addCleanup(cleanup);return p
    def line(self,p):
        with selectors.DefaultSelector() as s:
            s.register(p.stdout,selectors.EVENT_READ);self.assertTrue(s.select(timeout=8));return p.stdout.readline().strip()
    def crash(self,mode):
        p=self.child(mode);_,err=p.communicate(timeout=15);self.assertEqual(p.returncode,102 if mode=='crash_transaction' else 101,err)

    def test_process_exit_inside_copy_rolls_back_edge_prefix_selection_and_intent(self):
        before=self.h.config_path.read_bytes();self.crash('crash_transaction')
        self.assertIsNone(self.edge());self.assertIsNone(self.selected());self.assertEqual(self.rows(),[])
        self.assertEqual(self.count('session_coordination_ops'),0);self.assertEqual(self.h.config_path.read_bytes(),before)
        self.assertFalse(self.create().replayed)

    def test_missing_version_tables_are_read_only_at_preflight_and_ddl_rolls_back_on_crash(self):
        for table in ('web_message_versions','web_version_selections','web_session_forks'):
            self.h.conn.execute('DROP TABLE '+table)
        self.h.conn.execute("DELETE FROM messages WHERE json_extract(meta,'$.api_session')=?",(A,))
        def reset(legacy):
            config=legacy.load_config();config['sessions']=[r for r in config['sessions'] if r['id']!=A];legacy.save_config(config)
        self.c.mutate('fixture_without_versions',{},reset);self.request=self.snapshot()
        for table in ('web_message_versions','web_version_selections','web_session_forks'):
            self.assertFalse(self.h.conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone())
        self.crash('crash_transaction');self.assertEqual(self.generation(),1)
        for table in ('web_message_versions','web_version_selections','web_session_forks'):
            self.assertFalse(self.h.conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone())
        self.create();self.assertEqual(self.edge()['root_id'],CID);self.assertEqual(len(self.rows()),2)

    def test_recovery_before_publication_uses_original_copy_not_changed_source(self):
        before=self.h.config_path.read_bytes();self.crash('crash_before');copied=self.rows()
        self.assertEqual(self.h.config_path.read_bytes(),before);self.assertEqual(self.selected(),TARGET)
        self.h.conn.execute("UPDATE messages SET text='changed after intent' WHERE id=2")
        with patch.object(self.w,'_version_state',side_effect=AssertionError('must not recopy')):
            self.assertTrue(self.create(recover=True).replayed)
        self.assertEqual(self.rows(),copied);self.assertEqual(self.config()['sessions'][-1]['id'],TARGET)

    def test_damaged_prefix_edge_receipt_or_choice_refuses_without_compensation(self):
        self.crash('crash_before');before=self.h.config_path.read_bytes();message=self.rows()[0]
        cases=[('UPDATE messages SET text=? WHERE id=?',('damaged',message['id']),('UPDATE messages SET text=? WHERE id=?',(message['text'],message['id']))),
            ('UPDATE web_message_versions SET prompt_key=99 WHERE version_id=?',(TARGET,),('UPDATE web_message_versions SET prompt_key=3 WHERE version_id=?',(TARGET,))),
            ('UPDATE web_version_selections SET version_id=? WHERE root_id=?',(A,CID),('UPDATE web_version_selections SET version_id=? WHERE root_id=?',(TARGET,CID)))]
        for sql,args,(restore,values) in cases:
            self.h.conn.execute(sql,args);self.rejects(lambda:self.create(recover=True),'coordination_version_effect_changed')
            self.assertEqual(self.h.config_path.read_bytes(),before);self.h.conn.execute(restore,values)
        receipt=self.h.conn.execute('SELECT fingerprint FROM web_session_forks WHERE target=?',(TARGET,)).fetchone()[0]
        self.h.conn.execute("UPDATE web_session_forks SET fingerprint='bad' WHERE target=?",(TARGET,))
        self.rejects(lambda:self.create(recover=True),'coordination_version_effect_changed')
        self.h.conn.execute('UPDATE web_session_forks SET fingerprint=? WHERE target=?',(receipt,TARGET))
        self.assertTrue(self.create(recover=True).replayed)

    def test_pending_extra_ingress_or_private_context_refuses_without_deleting_it(self):
        self.crash('crash_before');before=self.h.config_path.read_bytes()
        mid=self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','unreviewed new input',?)",(STAMP,json.dumps({'api_session':TARGET}))).lastrowid
        self.rejects(lambda:self.create(recover=True),'coordination_version_effect_changed')
        self.assertEqual(self.h.config_path.read_bytes(),before);self.assertEqual(len(self.rows()),3)
        self.h.conn.execute('DELETE FROM messages WHERE id=?',(mid,))
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,1,?,?)',(TARGET,json.dumps(context.defaults()),STAMP))
        self.rejects(lambda:self.create(recover=True),'coordination_version_effect_changed')

    def test_pending_new_target_job_or_tool_namespace_refuses_without_compensation(self):
        self.crash('crash_before');before=self.h.config_path.read_bytes()
        self.h.conn.execute('CREATE TABLE web_mcp_actions(session_id TEXT)')
        self.h.conn.execute('INSERT INTO web_mcp_actions VALUES (?)',(TARGET,))
        self.rejects(lambda:self.create(recover=True),'coordination_version_namespace_conflict')
        self.assertEqual(self.h.config_path.read_bytes(),before);self.assertEqual(len(self.rows()),2)
        self.assertEqual(self.count('web_mcp_actions'),1)

    def test_published_recovery_does_not_republish_reselect_or_overwrite_later_history(self):
        self.crash('crash_after')
        self.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','later input',?)",(STAMP,json.dumps({'api_session':TARGET})))
        self.h.conn.execute('UPDATE web_version_selections SET version_id=? WHERE root_id=?',(A,CID));before=self.rows()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('no republish')):
            self.assertTrue(self.create(recover=True).replayed)
        self.assertEqual(self.rows(),before);self.assertEqual(self.selected(),A)

    def test_pending_exact_packet_current_auth_and_explicit_recovery_required(self):
        self.crash('crash_before')
        self.rejects(lambda:self.create(recover=True,title='other'),'coordination_request_conflict')
        self.rejects(lambda:self.create(w=writer(self.c,self.h.path,authorize=lambda *_:False),recover=True),'coordination_access_denied')
        self.rejects(self.snapshot,'coordination_recovery_required');self.rejects(self.create,'coordination_recovery_required')
        self.assertTrue(self.create(recover=True).replayed)

    def test_historical_replay_binds_actor_allocation_select_body_and_group(self):
        self.create()
        for kwargs in ({'access':ConfigCommandAccess('other')},{'title':'other'},{'stamp':'2026-11-06T00:00:01Z'},
                       {'select':False},{'body':{**BODY,'mode':'edit','message_id':3}},
                       {'request':{**self.request,'expected_group_ids':[CID]}}):
            self.rejects(lambda:self.create(**kwargs),'coordination_request_conflict')
        request=asdict(self.w.snapshot_selection(CID,{'version_id':CID},access=ACCESS))
        self.w.select_version(CID,{'version_id':CID},request_id='later_select',access=ACCESS,**request)
        config_commands(self.c).patch_session('rename',TARGET,{'title':'later title'},access=ACCESS)
        with patch.object(self.w,'_version_state',side_effect=AssertionError('no recopy')):receipt=self.create()
        self.assertEqual((receipt.generation,receipt.replayed),(1,True));self.assertEqual(self.selected(),CID)
        self.assertEqual(self.config()['sessions'][-1]['title'],'later title')

    def test_process_concurrency_replays_once_and_stale_other_request_loses(self):
        first=self.child('hold');self.assertEqual(self.line(first),'ready');same=self.child('same');other=self.child('stale')
        out,err=first.communicate('\n',timeout=15);sameout,sameerr=same.communicate(timeout=15);otherout,othererr=other.communicate(timeout=15)
        self.assertEqual((first.returncode,same.returncode,other.returncode),(0,0,0),err+sameerr+othererr)
        self.assertFalse(json.loads(out)['replayed']);self.assertTrue(json.loads(sameout)['replayed'])
        self.assertEqual(otherout.strip(),'coordination_cas_conflict');self.assertEqual(len(self.rows()),2)
        self.assertEqual(self.count('session_coordination_ops'),1)

    def test_journal_privacy_and_no_runtime_or_production_claim(self):
        self.create();raw=str([tuple(r) for r in self.h.conn.execute('SELECT * FROM session_coordination_ops')])
        for private in ('synthetic private','synthetic shared tone','synthetic secret',TITLE,STAMP):self.assertNotIn(private,raw)
        for flag in ('production_supported','http_integrated','all_writers_integrated','native_integrated',
                     'cross_store_atomic','version_creation_integrated','runtime_selection_confirmed'):self.assertFalse(getattr(self.w,flag))
