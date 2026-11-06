"""Pinned canonical connections and explicit private create/delete packets."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import RLock
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_coordination import SharedSessionCoordinator,digest,encode
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from backend.session_lifecycle_create_delete_packets import PrivateApiSessionCreate,PrivateApiSessionDelete
from backend.session_lifecycle_api_creation import CREATE
from backend.session_lifecycle_api_delete import DELETE
from backend.tests import test_session_lifecycle_api_creation as creations
from backend.tests import test_session_lifecycle_api_delete as deletions
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.tests.test_session_lifecycle_commands import commands as config_commands


def pinned(path):
    c=coordinator(path)
    return SharedSessionCoordinator(c._connect,c.config,c.directory,validate=c.validate,
        enabled=True,timeout=c.timeout,canonical_path=Path(path))


def binding(cls,c,directory,*,authorize=lambda *_:True):
    if cls is PrivateApiSessionCreate:w=creations.creator(c,authorize=authorize)
    else:w=deletions.writer(c,c.canonical_path,authorize=authorize)
    value=digest(encode([str(c.canonical_path),str(c.config),str(c.directory)]))
    p=PrivateCommandPackets(directory,binding=value,authorize=lambda *_:True,enabled=True)
    return cls(w,p,session_lock=w.session_lock,enabled=True)


class _Assertions:
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def execute(self,ref,**kw):return self.a.execute(self.sid,ref,access=kw.pop('access',self.access),**kw)
    def reopen(self,authorize=lambda *_:True):return binding(self.cls,pinned(self.f.h.path),self.directory,authorize=authorize)
    def test_default_off_and_global_denied_precede_io(self):
        off=self.cls(None,None,session_lock=None)
        self.rejects(lambda:off.execute(self.sid,PacketReference('x','0'*64),access=self.access),'lifecycle_disabled')
        self.a.commands._authorize=lambda *_:False;self.a.packets.directory=Path('/missing')
        self.rejects(self.prepare,'coordination_access_denied')
    def test_pin_namespace_and_same_lock_are_required(self):
        self.a.session_lock=RLock();self.rejects(self.prepare,'coordination_lock_mismatch')
        self.a.session_lock=self.a.commands.session_lock;self.a.packets.binding='0'*64
        self.rejects(self.prepare,'coordination_resource_mismatch')
        self.a=self.reopen();self.a.commands.coordinator.canonical_path=None
        self.rejects(self.prepare,'coordination_resource_mismatch');self.assertEqual(list(self.directory.iterdir()),[])
    def test_expected_identity_and_private_file_mode_bound(self):
        ref=self.prepare()
        self.rejects(lambda:self.execute(ref,access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.execute(PacketReference(ref.request_id,'0'*64)),'coordination_packet_changed')
        self.rejects(lambda:self.execute(ref,recover=1),'coordination_basic_api_packet_invalid')
        next(self.directory.glob('*.json')).chmod(0o644)
        self.rejects(lambda:self.execute(ref),'coordination_packet_private_file_required')
    def test_prepared_only_recovery_and_storage_denial_do_not_start_commands(self):
        ref=self.prepare();self.rejects(lambda:self.execute(ref,recover=True),'coordination_request_missing')
        self.a.packets.authorize=lambda *_:False
        self.rejects(lambda:self.execute(ref),'coordination_access_denied')
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)
    def test_packet_cannot_inject_privilege_dispatch_or_new_request_id(self):
        ref=self.prepare();p=self.a.packets.load(ref.request_id,self.a.action,self.sid,expected_sha=ref.packet_sha,access=self.access)
        for extra in ({'access':{'principal_id':'admin'}},{'recover':True},{'method':'other'}):
            with patch.object(self.a.packets,'load',return_value={**p,**extra}):
                self.rejects(lambda:self.execute(ref),'coordination_basic_api_packet_invalid')
        with patch.object(self.a.packets,'load',return_value={**p,'request_id':'different'}):
            self.rejects(lambda:self.execute(ref),'coordination_basic_api_packet_invalid')
    def test_new_handlers_require_actual_transaction_connection_to_match_pin(self):
        ref=self.prepare();self.a.commands.coordinator.canonical_path=self.f.root/'different.sqlite'
        self.a.packets.binding=digest(encode([str(self.a.commands.coordinator.canonical_path),str(self.f.h.config_path),str(self.f.root/'gate')]))
        self.rejects(self.prepare,'coordination_resource_mismatch')
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)
    def test_flags_remain_off(self):
        for flag in ('production_supported','http_integrated','automatic_recovery_supported'):self.assertFalse(getattr(self.a,flag))


@unittest.skipUnless(os.name=='posix','private POSIX API create')
class CreationPacketTests(_Assertions,unittest.TestCase):
    def setUp(self):
        self.f=creations.ApiCreationTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.cls=PrivateApiSessionCreate;self.sid=creations.TARGET;self.access=creations.ACCESS
        self.a=self.reopen()
    def prepare(self,activate=True):
        return self.a.prepare(session_id=self.sid,request_id='create-packet',title=creations.TITLE,
            created_at=creations.STAMP,activate=activate,access=self.access)
    def test_prepare_retains_only_original_row_parameters_and_metadata_cas(self):
        raw=self.f.h.config_path.read_bytes();rows=self.f.h.conn.execute('SELECT * FROM messages').fetchall();ref=self.prepare()
        self.assertEqual(self.prepare(),ref);self.assertEqual(self.f.h.config_path.read_bytes(),raw)
        self.assertEqual(self.f.h.conn.execute('SELECT * FROM messages').fetchall(),rows)
        p=self.a.packets.load(ref.request_id,CREATE,self.sid,expected_sha=ref.packet_sha,access=self.access)
        self.assertEqual(p['preconditions'],{k:self.f.request[k] for k in ('expected_generation','expected_snapshot_sha')})
        self.assertEqual((p['title'],p['created_at'],p['activate']),(creations.TITLE,creations.STAMP,True))
        self.assertNotIn('synthetic secret',str(p))
    def test_create_once_no_repreflight_or_new_id_allocation(self):
        ref=self.prepare(activate=False)
        with patch.object(self.a.commands,'snapshot_sessions',side_effect=AssertionError('no new snapshot')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(self.f.current()['active_session'],creations.CID)
        self.assertEqual(len([r for r in self.f.current()['sessions'] if r['id']==self.sid]),1)
    def test_target_write_and_existing_metadata_read_scopes_both_required(self):
        self.a.commands._authorize=lambda _a,action,sid:action!=CREATE or sid!=self.sid
        self.rejects(self.prepare,'coordination_access_denied')
        self.a.commands._authorize=lambda _a,action,sid:action==CREATE or sid!=creations.CID
        self.rejects(self.prepare,'coordination_access_denied');self.assertEqual(list(self.directory.iterdir()),[])
    def test_changed_activate_or_metadata_cannot_silently_replace_packet(self):
        ref=self.prepare();self.rejects(lambda:self.prepare(False),'coordination_packet_conflict')
        config_commands(self.a.commands.coordinator).patch_session('rename',creations.CID,{'title':'changed'},access=self.access)
        self.rejects(lambda:self.execute(ref),'coordination_cas_conflict')
    def test_actual_process_exit_reopens_original_creation_and_replays_once(self):
        ref=self.prepare();code="""import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_create_delete_packets import binding,pinned,PrivateApiSessionCreate,creations
from backend.session_lifecycle_request_packets import PacketReference
a=binding(PrivateApiSessionCreate,pinned(sys.argv[1]),Path(sys.argv[2]))
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')),patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(124)):
    a.execute(creations.TARGET,PacketReference(**json.loads(sys.stdin.read())),access=creations.ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,124,p.stderr);self.assertFalse(any(r['id']==self.sid for r in self.f.current()['sessions']))
        a=self.reopen()
        with patch.object(a.commands,'snapshot_sessions',side_effect=AssertionError('no new metadata snapshot')):
            self.assertTrue(a.execute(self.sid,ref,access=self.access,recover=True).replayed)
        self.assertTrue(self.execute(ref).replayed);self.assertEqual(len([r for r in self.f.current()['sessions'] if r['id']==self.sid]),1)
    def test_pending_revocation_refuses_publication(self):
        ref=self.prepare();old=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        a=self.reopen(authorize=lambda _a,_b,sid:sid!=self.sid)
        self.rejects(lambda:a.execute(self.sid,ref,access=self.access,recover=True),'coordination_access_denied')
        self.assertEqual(self.f.h.config_path.read_bytes(),old)


@unittest.skipUnless(os.name=='posix','private POSIX API delete')
class DeletePacketTests(_Assertions,unittest.TestCase):
    def setUp(self):
        self.f=deletions.ApiDeleteTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.cls=PrivateApiSessionDelete;self.sid=deletions.CID;self.access=deletions.ACCESS
        self.a=self.reopen()
    def prepare(self,next_active=deletions.NEXT):
        return self.a.prepare(self.sid,request_id='delete-packet',deleted_at=deletions.STAMP,next_active=next_active,access=self.access)
    def test_prepare_retains_exact_erasure_plan_without_erasure(self):
        raw=self.f.h.config_path.read_bytes();rows=self.f.rows();schema=self.f.schema();ref=self.prepare()
        self.assertEqual(self.prepare(),ref);self.assertEqual(self.f.h.config_path.read_bytes(),raw)
        self.assertEqual(self.f.rows(),rows);self.assertEqual(self.f.schema(),schema)
        p=self.a.packets.load(ref.request_id,DELETE,self.sid,expected_sha=ref.packet_sha,access=self.access)
        self.assertEqual(p['preconditions'],self.f.request);self.assertNotIn('synthetic private',str(p))
    def test_erase_once_and_historical_receipt_without_resnapshot(self):
        ref=self.prepare()
        with patch.object(self.a.commands,'snapshot_delete',side_effect=AssertionError('no new snapshot')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(len(self.f.rows()),1);self.assertEqual(self.f.config()['active_session'],deletions.NEXT)
    def test_fallback_scope_and_changed_content_prevent_execution(self):
        self.a.commands._authorize=lambda _a,_b,sid:sid!=deletions.NEXT
        self.rejects(self.prepare,'coordination_access_denied');self.a=self.reopen();ref=self.prepare()
        self.f.h.conn.execute("UPDATE messages SET text='changed' WHERE id=2")
        self.rejects(lambda:self.execute(ref),'coordination_delete_state_changed')
    def test_actual_process_exit_recovers_original_erasure_without_repeating_it(self):
        ref=self.prepare();code="""import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_create_delete_packets import binding,pinned,PrivateApiSessionDelete,deletions
from backend.session_lifecycle_request_packets import PacketReference
a=binding(PrivateApiSessionDelete,pinned(sys.argv[1]),Path(sys.argv[2]))
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')),patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(125)):
    a.execute(deletions.CID,PacketReference(**json.loads(sys.stdin.read())),access=deletions.ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,125,p.stderr);self.assertEqual(len(self.f.rows()),1)
        a=self.reopen()
        with patch('backend.session_lifecycle_api_delete._delete_ids',side_effect=AssertionError('no repeat erasure')):
            self.assertTrue(a.execute(self.sid,ref,access=self.access,recover=True).replayed)
        self.assertEqual(len(self.f.config()['deleted_sessions']),1)
    def test_pending_new_input_or_revocation_refuses_publication(self):
        ref=self.prepare();old=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        a=self.reopen(authorize=lambda _a,_b,sid:sid!=deletions.NEXT)
        self.rejects(lambda:a.execute(self.sid,ref,access=self.access,recover=True),'coordination_access_denied')
        self.f.h.conn.execute("INSERT INTO messages VALUES (60,?,'in','user','new input',?)",(deletions.STAMP,json.dumps({'api_session':self.sid})))
        self.rejects(lambda:self.reopen().execute(self.sid,ref,access=self.access,recover=True),'coordination_delete_effect_changed')
        self.assertEqual(self.f.h.config_path.read_bytes(),old);self.assertEqual(len(self.f.rows()),2)
    def test_fk_redaction_plan_survives_private_packet_recovery(self):
        self.f.h.conn.execute('CREATE TABLE refs(mid INTEGER REFERENCES messages(id))');self.f.h.conn.execute('INSERT INTO refs VALUES (2)')
        ref=self.prepare();self.execute(ref)
        self.assertEqual(self.f.h.conn.execute('SELECT text FROM messages WHERE id=2').fetchone()[0],'')
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM refs').fetchone()[0],1)
    def test_after_publication_recovery_preserves_later_bypass_input(self):
        ref=self.prepare();from backend.deployment_config import atomic_write_text
        def published(*args):atomic_write_text(*args);raise SystemExit('synthetic after publication')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=published):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute("INSERT INTO messages VALUES (60,?,'in','user','later input',?)",(deletions.STAMP,json.dumps({'api_session':self.sid})))
        before=self.f.rows();self.assertTrue(self.reopen().execute(self.sid,ref,access=self.access,recover=True).replayed)
        self.assertEqual(self.f.rows(),before)


@unittest.skipUnless(os.name=='posix','canonical connection pin')
class CanonicalPinTests(unittest.TestCase):
    def setUp(self):
        self.f=creations.ApiCreationTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
    def test_optional_pin_keeps_existing_metadata_binding_and_default_mode(self):
        self.assertIsNone(self.f.c.canonical_path);self.assertFalse(pinned(self.f.h.path).bootstrap())
        self.assertEqual(creations.creator(pinned(self.f.h.path)).snapshot_sessions(access=creations.ACCESS).generation,0)
    def test_wrong_and_relative_pin_fail_before_any_intent(self):
        for path in (self.f.root/'wrong.sqlite',Path('relative')):
            c=pinned(self.f.h.path);c.canonical_path=path
            with self.assertRaises(LifecycleError) as e:creations.creator(c).snapshot_sessions(access=creations.ACCESS)
            self.assertEqual(e.exception.category,'coordination_resource_mismatch')
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)
    def test_swapping_to_another_legitimate_database_is_rejected_on_each_connection(self):
        other=self.f.root/'other.sqlite';dst=sqlite3.connect(other);self.f.h.conn.backup(dst);dst.close()
        # The copied coordinator metadata would otherwise retain its old binding.
        c=pinned(other);conn=sqlite3.connect(other,isolation_level=None)
        # Use the original config/gate so B is a valid independent installation.
        c.config=self.f.h.config_path
        new_binding=digest(encode([str(other),str(c.config),str(c.directory)]))
        conn.execute('UPDATE session_coordination_meta SET binding=?',(new_binding,));conn.close()
        self.assertEqual(creations.creator(c).snapshot_sessions(access=creations.ACCESS).generation,0)
        from backend import channel_store
        expected=pinned(self.f.h.path);expected._connect=lambda:channel_store.connect(other)
        with self.assertRaises(LifecycleError) as e:creations.creator(expected).snapshot_sessions(access=creations.ACCESS)
        self.assertEqual(e.exception.category,'coordination_resource_mismatch')
