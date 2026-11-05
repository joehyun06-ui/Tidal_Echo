"""Synthetic-only transactional API branch copies and recoverable publication."""
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

from backend import deployment_config, web_session_context
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_api_fork import CoordinatedApiForkCommands, READ, FORK
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.web_session_fork import fork_conversation, ForkError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands

RID='12345678-1234-4234-8234-123456789abc'
TARGET='api-fork-'+RID.replace('-','')
STAMP='2026-11-05T14:00:00Z'
TITLE='synthetic branch'
ACCESS=ConfigCommandAccess('synthetic_owner')
BODY={'request_id':RID,'message_id':4,'mode':'branch'}


def writer(c, path, *, authorize=lambda *_:True, history=lambda _:None, enabled=True):
    return CoordinatedApiForkCommands(c,canonical_path=path,session_lock=RLock(),authorize=authorize,
        historical_provider=history,enabled=enabled)


def worker(path, mode):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
        request=json.loads(sys.stdin.readline());c=coordinator(path);w=writer(c,path)
        body={**BODY}
        if mode=='stale':body['request_id']='22345678-1234-4234-8234-123456789abc'
        if mode=='hold':
            original=w._source
            def source(*args):
                value=original(*args);print('ready',flush=True);sys.stdin.readline();return value
            w._source=source
        if mode=='crash_transaction':
            connect=c._connect
            class Proxy:
                def __init__(self,conn):self.conn=conn
                def __getattr__(self,n):return getattr(self.conn,n)
                def execute(self,sql,*args):
                    result=self.conn.execute(sql,*args)
                    if sql.startswith('INSERT INTO messages'):os._exit(78)
                    return result
            c._connect=lambda:Proxy(connect())
        run=lambda:w.fork_conversation(CID,body,title=TITLE,created_at=STAMP,access=ACCESS,**request)
        if mode in {'crash_before','crash_after'}:
            original=deployment_config.atomic_write_text
            def publish(*args):
                if mode=='crash_after':original(*args)
                os._exit(77)
            with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish):run()
        else:
            try:receipt=run()
            except LifecycleError as error:print(error.category,flush=True)
            else:print(json.dumps({'generation':receipt.generation,'replayed':receipt.replayed}),flush=True)


@unittest.skipUnless(os.name=='posix','private coordinator requires POSIX')
class ApiForkTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.h=FileHarness(self.root/'relay.sqlite');self.addCleanup(self.h.conn.close)
        config=self.h.get('legacy');config['active_session']=CID
        config['sessions'][0]['extension']={'synthetic':'preserve'}
        config['main_chain']=[{'model':'synthetic','url':'https://synthetic.invalid','key':'synthetic secret'}]
        self.h.put('legacy',config)
        for mid,direction,kind in [(2,'out','reply'),(3,'in','user'),(4,'out','reply')]:
            meta={'api_session':CID,'generation_id':'private usage','api':{'usage':{'total_tokens':20}}}
            if mid==3:meta['attachments']=[{'url':'/uploads/synthetic.png','kind':'image'}]
            self.h.conn.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                (mid,STAMP,direction,kind,'synthetic text '+str(mid),json.dumps(meta)))
        (self.root/'gate').mkdir(mode=0o700)
        self.c=coordinator(self.h.path);self.c.bootstrap();self.w=writer(self.c,self.h.path)
        self.request=asdict(self.w.snapshot_fork(CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS))
        self.legacy=SimpleNamespace(LOOP_CONFIG=self.h.config_path,load_config=lambda:self.h.get('legacy'))
        self.authority=WebSessionProviderAuthority(self.legacy)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline')))

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category)
        self.assertEqual(str(caught.exception),category)
        self.assertNotIn('synthetic text',repr(caught.exception))

    def fork(self,*,w=None,body=None,request=None,recover=False,access=ACCESS,title=TITLE,stamp=STAMP):
        return (w or self.w).fork_conversation(CID,BODY if body is None else body,title=title,
            created_at=stamp,access=access,recover=recover,**(self.request if request is None else request))

    def snapshot(self):return asdict(self.w.snapshot_fork(CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS))
    def config(self):return self.h.get('legacy')
    def rows(self,sid=TARGET):
        return [tuple(r) for r in self.h.conn.execute(
            "SELECT * FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id",(sid,))]
    def count(self,table):return self.h.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def schema(self):return self.h.conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()

    def test_disabled_and_denied_have_no_io(self):
        def forbidden(*_):self.fail('dependency touched')
        w=writer(None,self.h.path,authorize=forbidden,history=forbidden,enabled=False)
        self.rejects(lambda:self.fork(w=w),'lifecycle_disabled')
        self.rejects(lambda:w.snapshot_fork(CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS),'lifecycle_disabled')
        self.rejects(lambda:self.fork(w=writer(None,self.h.path,authorize=lambda *_:False)),
            'coordination_access_denied')

    def test_snapshot_is_query_only_bounded_and_does_not_initialize_tables(self):
        config=self.h.config_path.read_bytes();schema=self.schema();rows=self.rows(CID)
        value=self.snapshot();self.assertEqual(value,self.request)
        self.assertEqual(set(value),{'expected_generation','expected_snapshot_sha','expected_source_sha','expected_copy_sha'})
        self.assertEqual(self.schema(),schema);self.assertEqual(self.rows(CID),rows)
        self.assertEqual(self.h.config_path.read_bytes(),config);self.assertEqual(self.count('session_coordination_ops'),0)
        def forbidden(_legacy,_generation,conn):conn.execute("DELETE FROM messages")
        self.rejects(lambda:self.c.observe_canonical(forbidden),'coordination_unavailable')

    def test_copy_context_and_intent_commit_together_before_real_json_publication(self):
        source=self.rows(CID);old=self.config();publish=deployment_config.atomic_write_text
        def inspect(path,text):
            self.assertEqual(len(self.rows()),4)
            self.assertEqual(self.h.conn.execute('SELECT status FROM session_coordination_ops').fetchone()[0],'pending')
            self.assertEqual(self.count('web_session_forks'),1);self.assertEqual(self.count('web_session_context'),1)
            self.assertEqual(self.config(),old);publish(path,text)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=inspect):receipt=self.fork()
        self.assertEqual((receipt.generation,receipt.replayed),(1,False))
        expected=deepcopy(old);expected['sessions'].append({'id':TARGET,'title':TITLE,'since_id':0,
            'created_at':STAMP,'pinned':False,'provider':'api'})
        self.assertEqual(self.config(),expected);self.assertEqual(self.rows(CID),source)
        for index,row in enumerate(self.rows(),1):
            meta=json.loads(row[-1]);self.assertEqual(meta['branch_origin'],{'session_id':CID,'message_id':index})
            self.assertNotIn('generation_id',meta);self.assertNotIn('api',meta)
        self.assertEqual(json.loads(self.rows()[2][-1])['attachments'],[{'url':'/uploads/synthetic.png','kind':'image'}])

    def test_explicit_real_fork_entry_returns_receipt_and_replays_once(self):
        kwargs={'relay_db':self.h.path,'coordinated':self.w,'session_lock':self.w.session_lock,
            'title':TITLE,'created_at':STAMP,'access':ACCESS,**self.request}
        first=fork_conversation(self.authority,CID,BODY,**kwargs)
        self.assertFalse(first.replayed);self.assertTrue(fork_conversation(self.authority,CID,BODY,**kwargs).replayed)
        self.assertEqual(len(self.rows()),4);self.assertEqual(len(self.config()['sessions']),2)

    def test_entry_missing_dependency_or_wrong_resource_refuses_before_legacy(self):
        with self.assertRaises(ForkError) as caught:
            fork_conversation(self.authority,CID,BODY,relay_db=self.h.path,title=TITLE)
        self.assertEqual(caught.exception.category,'fork_coordination_required')
        self.rejects(lambda:fork_conversation(self.authority,CID,BODY,relay_db='/wrong.sqlite',coordinated=self.w,
            session_lock=self.w.session_lock),
            'coordination_resource_mismatch')
        self.legacy.LOOP_CONFIG=Path('/wrong.json')
        self.rejects(lambda:fork_conversation(self.authority,CID,BODY,relay_db=self.h.path,coordinated=self.w,
            session_lock=self.w.session_lock),
            'coordination_resource_mismatch')

    def test_fork_entry_requires_the_exact_injected_session_lock(self):
        for lock in (None,RLock()):
            self.rejects(lambda:fork_conversation(self.authority,CID,BODY,relay_db=self.h.path,
                coordinated=self.w,session_lock=lock),'coordination_lock_mismatch')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_actual_connect_must_match_declared_database(self):
        wrong=writer(self.c,self.root/'other.sqlite')
        self.rejects(lambda:self.fork(w=wrong),'coordination_resource_mismatch')
        self.assertEqual(self.count('session_coordination_ops'),0)

    def test_global_source_target_authorization_is_current(self):
        for denied in (None,CID,TARGET):
            w=writer(self.c,self.h.path,authorize=lambda _a,_b,target:target!=denied,
                history=lambda _:self.fail('history queried before scope authorization'))
            self.rejects(lambda:self.fork(w=w),'coordination_access_denied')
        self.fork()
        self.rejects(lambda:self.fork(w=writer(self.c,self.h.path,authorize=lambda *_:False)),
            'coordination_access_denied')

    def test_revocation_during_canonical_copy_rolls_back_sql_and_intent(self):
        state={'allowed':True};w=writer(self.c,self.h.path,authorize=lambda *_:state['allowed'])
        source=w._source
        def revoke(*args):
            value=source(*args);state['allowed']=False;return value
        w._source=revoke
        self.rejects(lambda:self.fork(w=w),'coordination_access_denied')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)
        self.assertFalse(self.h.conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_session_forks'").fetchone())

    def test_stale_generation_and_metadata_digest_refuse(self):
        config_commands(self.c).patch_session('other',CID,{'title':'changed'},access=ACCESS)
        self.rejects(self.fork,'coordination_cas_conflict')
        req={**self.request,'expected_generation':1}
        self.rejects(lambda:self.fork(request=req),'coordination_cas_conflict')
        self.assertEqual(self.rows(),[])

    def test_source_text_timestamp_metadata_changes_refuse_without_intent(self):
        original=self.rows(CID)[1]
        for column,value in [('text','changed'),('ts','changed'),('meta',json.dumps({'api_session':CID,'changed':True}))]:
            self.h.conn.execute('UPDATE messages SET '+column+'=? WHERE id=2',(value,))
            self.rejects(self.fork,'coordination_fork_source_changed')
            self.h.conn.execute('UPDATE messages SET '+column+'=? WHERE id=2',(original[{'ts':1,'text':4,'meta':5}[column]],))
        self.assertEqual(self.count('session_coordination_ops'),0);self.assertEqual(self.rows(),[])

    def test_context_is_bound_and_validated_and_copied_with_fixed_time(self):
        config={**web_session_context.defaults(),'tone':'synthetic source tone'}
        web_session_context.save(self.h.path,CID,{'revision':0,'config':config})
        self.rejects(self.fork,'coordination_fork_source_changed');self.request=self.snapshot()
        self.fork();context=web_session_context.get(self.h.path,TARGET)
        self.assertEqual(context['config'],config);self.assertEqual(context['updated_at'],STAMP)

    def test_message_prefix_does_not_include_later_source_messages(self):
        self.h.conn.execute("INSERT INTO messages VALUES (5,?,'in','user','later',?)",(STAMP,json.dumps({'api_session':CID})))
        self.fork();self.assertEqual(len(self.rows()),4);self.assertEqual(len(self.rows(CID)),5)

    def test_native_and_version_alternatives_are_not_supported(self):
        def change(legacy,provider):
            config=legacy.load_config();config['sessions'][0]['provider']=provider;legacy.save_config(config)
        self.c.mutate('fixture_codex',{},lambda legacy:change(legacy,'codex'))
        value=self.w.snapshot_sessions(access=ACCESS)
        self.request.update(expected_generation=value.generation,expected_snapshot_sha=value.snapshot_sha)
        self.rejects(self.fork,'coordination_native_fork_unsupported')
        self.c.mutate('fixture_api',{},lambda legacy:change(legacy,'api'))
        self.request=self.snapshot()
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,CID))
        self.rejects(self.fork,'coordination_fork_versions_unsupported')
        for mode in ('edit','regenerate'):
            self.rejects(lambda:self.fork(body={**BODY,'mode':mode}),'coordination_fork_mode_unsupported')

    def test_bad_request_and_missing_allocation_refuse(self):
        for body in ({**BODY,'message_id':True},{**BODY,'request_id':'bad'}, {**BODY,'extra':1}):
            self.rejects(lambda:self.fork(body=body),'coordination_fork_invalid')
        self.rejects(lambda:self.fork(stamp=None),'coordination_creation_invalid')
        self.rejects(lambda:self.fork(body={**BODY,'message_id':999}),'coordination_fork_message_unavailable')
        self.rejects(lambda:self.fork(request={**self.request,'expected_copy_sha':'bad'}),'coordination_fork_invalid')

    def test_target_orphan_and_bad_schema_refuse(self):
        self.h.conn.execute("INSERT INTO messages VALUES (8,?,'in','user','orphan',?)",(STAMP,json.dumps({'api_session':TARGET})))
        self.rejects(self.fork,'coordination_session_namespace_conflict')
        self.h.conn.execute('DELETE FROM messages WHERE id=8')
        self.h.conn.execute('CREATE VIEW web_session_forks AS SELECT 1 AS target')
        self.rejects(self.fork,'coordination_namespace_invalid')

    def test_live_tombstone_and_historical_target_are_never_reused(self):
        w=writer(self.c,self.h.path,history=lambda sid:'api' if sid==TARGET else None)
        self.rejects(lambda:self.fork(w=w),'coordination_session_conflict')
        def tombstone(legacy):
            config=legacy.load_config();config['deleted_sessions']=[{'id':TARGET,'provider':'api','deleted_at':STAMP}]
            legacy.save_config(config)
        self.c.mutate('fixture_tombstone',{},tombstone)
        value=self.w.snapshot_sessions(access=ACCESS)
        self.request.update(expected_generation=value.generation,expected_snapshot_sha=value.snapshot_sha)
        self.rejects(self.fork,'coordination_session_conflict')
        self.assertEqual(self.rows(),[])

    def test_snapshot_requires_read_scope_for_source_and_target(self):
        for denied in (None,CID,TARGET):
            w=writer(self.c,self.h.path,authorize=lambda _a,action,target:action!=READ or target!=denied,
                history=lambda _:self.fail('history queried before read scope authorization'))
            self.rejects(lambda:w.snapshot_fork(CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS),
                'coordination_access_denied')

    def test_optional_table_wrong_columns_or_trigger_refuse_before_writing(self):
        self.h.conn.execute('CREATE TABLE web_session_context(root_id TEXT PRIMARY KEY,config TEXT)')
        self.rejects(self.fork,'coordination_fork_schema_invalid')
        self.h.conn.execute('DROP TABLE web_session_context')
        web_session_context.get(self.h.path,CID)
        self.h.conn.execute('CREATE TRIGGER synthetic_context_trigger AFTER INSERT ON web_session_context BEGIN SELECT 1; END')
        self.rejects(self.fork,'coordination_fork_schema_invalid')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_corrupt_source_context_and_duplicate_json_fields_refuse(self):
        web_session_context.get(self.h.path,CID)
        self.h.conn.execute('INSERT INTO web_session_context VALUES (?,1,?,?)',(CID,'{"bad":1}',STAMP))
        self.rejects(self.fork,'coordination_fork_source_invalid')
        self.h.conn.execute('DELETE FROM web_session_context')
        self.h.conn.execute('UPDATE messages SET meta=? WHERE id=2',('{"api_session":"'+CID+'","api_session":"'+CID+'"}',))
        self.rejects(self.fork,'coordination_fork_source_invalid')

    def test_copy_digest_is_checked_independently_of_source_digest(self):
        self.rejects(lambda:self.fork(request={**self.request,'expected_copy_sha':'0'*64}),
            'coordination_fork_source_changed')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_source_change_between_buffer_and_intent_is_observed_in_canonical_transaction(self):
        validate=self.c.validate
        def change(config):
            validate(config);self.h.conn.execute("UPDATE messages SET text='late source change' WHERE id=2")
        self.c.validate=change
        self.rejects(self.fork,'coordination_fork_source_changed')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_history_row_and_byte_limits_refuse_without_publication(self):
        self.h.conn.execute("UPDATE messages SET text=? WHERE id=2",('x'*(8*1024*1024),))
        self.rejects(self.fork,'coordination_fork_history_too_large')
        self.h.conn.execute("UPDATE messages SET text='small' WHERE id=2")
        self.h.conn.execute('BEGIN')
        try:
            self.h.conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                ((n,STAMP,'in','user','synthetic',json.dumps({'api_session':CID})) for n in range(5,5002)))
            self.h.conn.commit()
        except BaseException:self.h.conn.rollback();raise
        self.rejects(lambda:self.fork(body={**BODY,'message_id':5001}),'coordination_fork_history_too_large')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)

    def test_copy_callback_failure_rolls_back_all_canonical_effects(self):
        source=self.w._source
        def broken(*args):
            sha,copy_sha,rows,context=source(*args)
            rows[1]['text']=None  # actual messages NOT NULL failure after the first insert
            return sha,copy_sha,rows,context
        self.w._source=broken
        self.rejects(self.fork,'coordination_unavailable')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)
        self.assertFalse(self.h.conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_session_context'").fetchone())

    def test_history_receipt_replays_after_source_and_target_change(self):
        self.fork();self.h.conn.execute("UPDATE messages SET text='source later revision' WHERE id=2")
        self.h.conn.execute("UPDATE messages SET text='target later revision' WHERE id>4")
        config_commands(self.c).patch_session('rename',TARGET,{'title':'later'},access=ACCESS)
        before=self.rows()
        with patch.object(self.w,'_source',side_effect=AssertionError('must not copy again')):receipt=self.fork()
        self.assertEqual((receipt.generation,receipt.replayed),(1,True));self.assertEqual(self.rows(),before)
        self.assertEqual(self.config()['sessions'][-1]['title'],'later')

    def test_same_request_different_actor_allocation_or_digest_conflicts(self):
        self.fork()
        for kwargs in ({'access':ConfigCommandAccess('other')},{'title':'other'}, {'stamp':'2026-11-05T14:00:01Z'},
                       {'request':{**self.request,'expected_source_sha':'0'*64}}, {'body':{**BODY,'message_id':3}}):
            self.rejects(lambda:self.fork(**kwargs),'coordination_request_conflict')

    def child(self,mode,request=None):
        p=subprocess.Popen([sys.executable,'-c',
            'import sys;from backend.tests.test_session_lifecycle_api_fork import worker;worker(sys.argv[1],sys.argv[2])',
            self.h.path,mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        p.stdin.write(json.dumps(self.request if request is None else request)+'\n');p.stdin.flush()
        def cleanup():
            if p.poll() is None:p.kill()
            p.communicate(timeout=5)
        self.addCleanup(cleanup);return p

    def line(self,p):
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(timeout=8))
            return p.stdout.readline().strip()

    def crash(self,mode):
        p=self.child(mode);_,error=p.communicate(timeout=15)
        self.assertEqual(p.returncode,78 if mode=='crash_transaction' else 77,error)

    def test_process_exit_inside_copy_rolls_back_ddl_rows_and_intent(self):
        old=self.h.config_path.read_bytes();schema=self.schema();self.crash('crash_transaction')
        self.assertEqual(self.rows(),[]);self.assertEqual(self.count('session_coordination_ops'),0)
        self.assertEqual(self.schema(),schema);self.assertEqual(self.h.config_path.read_bytes(),old)
        self.assertFalse(self.fork().replayed)

    def test_before_publication_recovery_uses_copy_not_changed_source(self):
        old=self.h.config_path.read_bytes();self.crash('crash_before')
        self.assertEqual(self.h.config_path.read_bytes(),old);self.assertEqual(len(self.rows()),4)
        self.h.conn.execute("UPDATE messages SET text='changed source after intent' WHERE id=2")
        with patch.object(self.w,'_source',side_effect=AssertionError('must not reread source')):
            self.assertTrue(self.fork(recover=True).replayed)
        self.assertEqual(len(self.rows()),4);self.assertEqual(len(self.config()['sessions']),2)

    def test_pending_copy_damage_refuses_without_compensation(self):
        self.crash('crash_before');old=self.h.config_path.read_bytes()
        self.h.conn.execute("UPDATE messages SET text='damaged copy' WHERE id>4")
        self.rejects(lambda:self.fork(recover=True),'coordination_fork_copy_changed')
        self.assertEqual(len(self.rows()),4);self.assertEqual(self.h.config_path.read_bytes(),old)
        self.assertEqual(self.h.conn.execute('SELECT status FROM session_coordination_ops').fetchone()[0],'pending')

    def test_uncooperative_hidden_target_ingress_is_detected_before_publication(self):
        self.crash('crash_before');old=self.h.config_path.read_bytes()
        self.h.conn.execute("INSERT INTO messages VALUES (20,?,'in','user','unexpected ingress',?)",(STAMP,json.dumps({'api_session':TARGET})))
        self.rejects(lambda:self.fork(recover=True),'coordination_fork_copy_changed')
        self.assertEqual(self.h.config_path.read_bytes(),old);self.assertEqual(len(self.rows()),5)
        self.assertFalse(self.w.cross_store_atomic)

    def test_pending_receipt_and_context_damage_are_refused(self):
        self.crash('crash_before')
        original=self.h.conn.execute('SELECT fingerprint FROM web_session_forks').fetchone()[0]
        self.h.conn.execute("UPDATE web_session_forks SET fingerprint='bad'")
        self.rejects(lambda:self.fork(recover=True),'coordination_fork_copy_changed')
        self.h.conn.execute('UPDATE web_session_forks SET fingerprint=?',(original,))
        self.h.conn.execute('UPDATE web_session_context SET revision=2')
        self.rejects(lambda:self.fork(recover=True),'coordination_fork_copy_changed')

    def test_published_recovery_preserves_subsequent_history_and_context(self):
        self.crash('crash_after');self.assertEqual(len(self.config()['sessions']),2)
        self.h.conn.execute("INSERT INTO messages VALUES (20,?,'in','user','new target message',?)",(STAMP,json.dumps({'api_session':TARGET})))
        web_session_context.save(self.h.path,TARGET,{'revision':1,'config':{**web_session_context.defaults(),'tone':'later'}})
        before=self.rows()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('no republish')):
            self.assertTrue(self.fork(recover=True).replayed)
        self.assertEqual(self.rows(),before);self.assertEqual(web_session_context.get(self.h.path,TARGET)['config']['tone'],'later')

    def test_pending_recovery_requires_original_packet_and_current_authorization(self):
        self.crash('crash_before')
        self.rejects(lambda:self.fork(recover=True,title='changed'),'coordination_request_conflict')
        denied=writer(self.c,self.h.path,authorize=lambda *_:False)
        self.rejects(lambda:self.fork(w=denied,recover=True),'coordination_access_denied')
        self.assertTrue(self.fork(recover=True).replayed)

    def test_pending_blocks_new_snapshot_and_normal_request_until_explicit_recovery(self):
        self.crash('crash_before')
        self.rejects(self.snapshot,'coordination_recovery_required')
        self.rejects(self.fork,'coordination_recovery_required')
        self.assertTrue(self.fork(recover=True).replayed)

    def test_process_same_request_replays_one_copy_and_different_stale_request_loses(self):
        first=self.child('hold');self.assertEqual(self.line(first),'ready')
        same=self.child('replay');stale=self.child('stale')
        out,err=first.communicate('\n',timeout=15);self.assertEqual(first.returncode,0,err)
        sameout,sameerr=same.communicate(timeout=15);staleout,staleerr=stale.communicate(timeout=15)
        self.assertEqual((same.returncode,stale.returncode),(0,0),sameerr+staleerr)
        self.assertFalse(json.loads(out)['replayed']);self.assertTrue(json.loads(sameout)['replayed'])
        self.assertEqual(staleout.strip(),'coordination_cas_conflict');self.assertEqual(len(self.rows()),4)
        self.assertEqual(self.count('session_coordination_ops'),1)

    def test_journal_and_public_snapshot_do_not_expose_private_text_settings_or_routes(self):
        self.fork();raw=str([tuple(r) for r in self.h.conn.execute('SELECT * FROM session_coordination_ops')])
        raw+=str(self.request)
        for private in ('synthetic text',TITLE,STAMP,'synthetic secret','synthetic.invalid'):
            self.assertNotIn(private,raw)
        self.assertFalse(self.w.production_supported);self.assertFalse(self.w.http_integrated)
        self.assertFalse(self.w.all_writers_integrated);self.assertFalse(self.w.native_integrated)
