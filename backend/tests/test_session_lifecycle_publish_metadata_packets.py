"""Original caller CAS for metadata drafts and private API-row publication."""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_coordination import digest,encode
from backend.session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from backend.session_lifecycle_publish_metadata_packets import PrivateApiRowPublish,PrivateSessionMetadataSave
from backend.session_lifecycle_api_creation import PUBLISH
from backend.session_lifecycle_session_lists import SAVE
from backend.tests import test_session_lifecycle_api_creation as creations
from backend.tests import test_session_lifecycle_session_lists as lists
from backend.tests.test_session_lifecycle_create_delete_packets import pinned
from backend.tests.test_session_lifecycle_commands import commands as config_commands


def binding(cls,c,directory,*,authorize=lambda *_:True):
    if cls is PrivateApiRowPublish:w=creations.creator(c,authorize=authorize)
    else:w=lists.session_lists(c,authorize=authorize)
    value=digest(encode([str(c.canonical_path),str(c.config),str(c.directory)]))
    p=PrivateCommandPackets(directory,binding=value,authorize=lambda *_:True,enabled=True)
    return cls(w,p,session_lock=w.session_lock,enabled=True)


class _Assertions:
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def execute(self,ref,*,adapter=None,access=None,recover=False):
        a=adapter or self.a;access=self.access if access is None else access
        if self.target is None:return a.execute(ref,access=access,recover=recover)
        return a.execute(self.target,ref,access=access,recover=recover)
    def reopen(self,authorize=lambda *_:True):return binding(self.cls,pinned(self.f.h.path),self.directory,authorize=authorize)
    def test_default_off_and_denied_precede_io(self):
        off=self.cls(None,None,session_lock=None)
        self.rejects(lambda:self.execute(PacketReference('x','0'*64),adapter=off),'lifecycle_disabled')
        self.a.commands._authorize=lambda *_:False;self.a.packets.directory=Path('/missing')
        self.rejects(self.prepare,'coordination_access_denied')
    def test_matching_lock_pin_and_namespace_are_required(self):
        self.a.session_lock=RLock();self.rejects(self.prepare,'coordination_lock_mismatch')
        self.a.session_lock=self.a.commands.session_lock;self.a.packets.binding='0'*64
        self.rejects(self.prepare,'coordination_resource_mismatch')
        self.a=self.reopen();self.a.commands.coordinator.canonical_path=None
        self.rejects(self.prepare,'coordination_resource_mismatch')
    def test_actor_digest_private_mode_and_storage_permission_bound(self):
        ref=self.prepare()
        self.rejects(lambda:self.execute(ref,access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.execute(PacketReference(ref.request_id,'0'*64)),'coordination_packet_changed')
        self.a.packets.authorize=lambda *_:False
        self.rejects(lambda:self.execute(ref),'coordination_access_denied');self.a.packets.authorize=lambda *_:True
        next(self.directory.glob('*.json')).chmod(0o644)
        self.rejects(lambda:self.execute(ref),'coordination_packet_private_file_required')
    def test_prepared_only_recovery_cannot_start_mutation(self):
        ref=self.prepare();self.rejects(lambda:self.execute(ref,recover=True),'coordination_request_missing')
        self.assertEqual(self.f.generation(),0)
    def test_packet_cannot_supply_privilege_dispatch_or_different_request(self):
        ref=self.prepare();p=self.a.packets.load(ref.request_id,self.a.action,self.target,expected_sha=ref.packet_sha,access=self.access)
        for extra in ({'access':{'principal_id':'admin'}},{'method':'other'},{'recover':True}):
            with patch.object(self.a.packets,'load',return_value={**p,**extra}):
                self.rejects(lambda:self.execute(ref),'coordination_metadata_packet_invalid')
        with patch.object(self.a.packets,'load',return_value={**p,'request_id':'different'}):
            self.rejects(lambda:self.execute(ref),'coordination_metadata_packet_invalid')
        self.rejects(lambda:self.execute(ref,recover=1),'coordination_metadata_packet_invalid')
    def test_current_write_permission_still_required_for_saved_packet(self):
        ref=self.prepare();a=self.reopen(authorize=lambda _a,_b,sid:sid!=self.denied_member)
        self.rejects(lambda:self.execute(ref,adapter=a),'coordination_access_denied')
    def test_flags_remain_off(self):
        for flag in ('production_supported','http_integrated','native_integrated','automatic_recovery_supported'):
            self.assertFalse(getattr(self.a,flag))
    def test_cross_action_and_wrong_concrete_command_cannot_consume_reference(self):
        ref=self.prepare()
        other_cls=PrivateSessionMetadataSave if self.cls is PrivateApiRowPublish else PrivateApiRowPublish
        other=binding(other_cls,pinned(self.f.h.path),self.directory)
        if other_cls is PrivateSessionMetadataSave:
            self.rejects(lambda:other.execute(ref,access=self.access),'coordination_packet_conflict')
        else:
            self.rejects(lambda:other.execute(creations.TARGET,ref,access=self.access),'coordination_packet_conflict')
        self.a.commands=other.commands
        self.rejects(lambda:self.execute(ref),'coordination_metadata_packet_invalid')


@unittest.skipUnless(os.name=='posix','private POSIX publication')
class PublishPacketTests(_Assertions,unittest.TestCase):
    def setUp(self):
        self.f=creations.ApiCreationTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.cls=PrivateApiRowPublish;self.target=creations.TARGET;self.access=creations.ACCESS
        self.denied_member=self.target;self.a=self.reopen()
    def prepare(self,row=None,activate=False):
        return self.a.prepare(creations.new_row() if row is None else row,request_id='publish-packet',activate=activate,access=self.access)
    def test_prepare_has_no_publication_or_canonical_effect(self):
        raw=self.f.h.config_path.read_bytes();messages=self.f.h.conn.execute('SELECT * FROM messages').fetchall();ref=self.prepare()
        self.assertEqual(self.prepare(),ref);self.assertEqual(self.f.h.config_path.read_bytes(),raw)
        self.assertEqual(self.f.h.conn.execute('SELECT * FROM messages').fetchall(),messages);self.assertEqual(self.f.generation(),0)
        p=self.a.packets.load(ref.request_id,PUBLISH,self.target,expected_sha=ref.packet_sha,access=self.access)
        self.assertEqual(p['body'],{'row':creations.new_row(),'activate':False});self.assertNotIn('synthetic secret',str(p))
    def test_fixed_row_publishes_once_without_allocation_or_repreflight(self):
        ref=self.prepare()
        with patch.object(self.a.commands,'snapshot_sessions',side_effect=AssertionError('no new preflight')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(self.f.current()['active_session'],creations.CID)
        self.assertEqual(len([r for r in self.f.current()['sessions'] if r['id']==self.target]),1)
    def test_changed_row_or_activation_cannot_replace_original_packet(self):
        self.prepare();row={**creations.new_row(),'title':'different'}
        self.rejects(lambda:self.prepare(row),'coordination_packet_conflict')
        self.rejects(lambda:self.prepare(activate=True),'coordination_packet_conflict')
    def test_native_and_nonempty_rows_refuse_before_retention(self):
        self.rejects(lambda:self.prepare({**creations.new_row(),'provider':'codex'}),'coordination_native_creation_unsupported')
        self.rejects(lambda:self.prepare({**creations.new_row(),'since_id':1}),'coordination_creation_scope_unsupported')
        self.assertEqual(list(self.directory.iterdir()),[])
    def test_prepared_target_cannot_change_inside_envelope(self):
        ref=self.prepare();p=self.a.packets.load(ref.request_id,PUBLISH,self.target,expected_sha=ref.packet_sha,access=self.access)
        p['body']['row']['id']='another'
        with patch.object(self.a.packets,'load',return_value=p):
            self.rejects(lambda:self.execute(ref),'coordination_metadata_packet_invalid')
    def test_metadata_change_after_prepare_loses_cas(self):
        ref=self.prepare();config_commands(self.a.commands.coordinator).patch_session('later',creations.CID,{'title':'later title'},access=self.access)
        self.rejects(lambda:self.execute(ref),'coordination_cas_conflict')
    def test_actual_process_exit_reopens_original_row_publication(self):
        ref=self.prepare();code="""import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_publish_metadata_packets import binding,pinned,PrivateApiRowPublish,creations
from backend.session_lifecycle_request_packets import PacketReference
a=binding(PrivateApiRowPublish,pinned(sys.argv[1]),Path(sys.argv[2]))
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')),patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(126)):
    a.execute(creations.TARGET,PacketReference(**json.loads(sys.stdin.read())),access=creations.ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,126,p.stderr)
        a=self.reopen()
        with patch.object(a.commands,'snapshot_sessions',side_effect=AssertionError('no new snapshot')):
            self.assertTrue(self.execute(ref,adapter=a,recover=True).replayed)
        self.assertEqual(len([r for r in self.f.current()['sessions'] if r['id']==self.target]),1)
    def test_committed_replay_preserves_newer_row_metadata(self):
        ref=self.prepare();self.execute(ref)
        config_commands(self.a.commands.coordinator).patch_session('later-title',self.target,{'title':'later title'},access=self.access)
        self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(next(r for r in self.f.current()['sessions'] if r['id']==self.target)['title'],'later title')


@unittest.skipUnless(os.name=='posix','private POSIX metadata')
class MetadataPacketTests(_Assertions,unittest.TestCase):
    def setUp(self):
        self.f=lists.SessionListTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.cls=PrivateSessionMetadataSave;self.target=None;self.access=lists.ACCESS
        self.denied_member=lists.SID2;self.a=self.reopen()
        snap=self.a.commands.snapshot_sessions(access=self.access)
        self.rows=snap.as_rows();self.rows[0]['title']='retained edit';self.rows[1]['pinned']=True
        self.expected={'expected_generation':snap.generation,'expected_snapshot_sha':snap.snapshot_sha}
    def prepare(self,rows=None,active=None):
        return self.a.prepare(self.rows if rows is None else rows,active,request_id='metadata-packet',access=self.access,**self.expected)
    def test_prepare_checks_original_cas_without_saving_metadata(self):
        raw=self.f.h.config_path.read_bytes();messages=self.f.h.conn.execute('SELECT * FROM messages').fetchall();ref=self.prepare()
        self.assertEqual(self.prepare(),ref);self.assertEqual(self.f.h.config_path.read_bytes(),raw)
        self.assertEqual(self.f.h.conn.execute('SELECT * FROM messages').fetchall(),messages)
        p=self.a.packets.load(ref.request_id,SAVE,None,expected_sha=ref.packet_sha,access=self.access)
        self.assertEqual(p['preconditions'],self.expected);self.assertEqual(p['body']['rows'],self.rows)
        self.assertNotIn('synthetic secret',str(p));self.assertNotIn('synthetic_private',str(p))
    def test_stale_draft_is_rejected_before_retention_not_rebased_to_new_snapshot(self):
        config_commands(self.a.commands.coordinator).patch_session('newer',lists.CID,{'title':'newer title'},access=self.access)
        self.rejects(self.prepare,'coordination_cas_conflict');self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.f.current()['sessions'][0]['title'],'newer title')
    def test_state_change_after_retention_still_loses_original_cas(self):
        ref=self.prepare();config_commands(self.a.commands.coordinator).patch_session('later',lists.CID,{'title':'later title'},access=self.access)
        self.rejects(lambda:self.execute(ref),'coordination_cas_conflict')
        self.assertEqual(self.f.current()['sessions'][0]['title'],'later title')
    def test_topology_changes_refuse_before_packet_creation(self):
        self.rejects(lambda:self.prepare(list(reversed(self.rows))),'coordination_session_topology_unsupported')
        self.rejects(lambda:self.prepare(self.rows[:-1]),'coordination_session_topology_unsupported')
        self.assertEqual(list(self.directory.iterdir()),[])
    def test_each_row_needs_read_and_write_authorization(self):
        for action in (SAVE,lists.READ):
            self.a.commands._authorize=lambda _a,a,sid:a!=action or sid!=lists.SID2
            self.rejects(self.prepare,'coordination_access_denied')
        self.assertEqual(list(self.directory.iterdir()),[])
    def test_original_raw_extensions_and_active_pointer_are_preserved(self):
        old=self.f.current();ref=self.prepare();self.execute(ref);new=self.f.current()
        self.assertEqual(new['sessions'][0]['title'],'retained edit');self.assertTrue(new['sessions'][1]['pinned'])
        self.assertEqual(new['active_session'],old['active_session']);self.assertEqual(new['main_chain'],old['main_chain'])
        self.assertEqual(new['sessions'][0]['extension'],old['sessions'][0]['extension'])
        self.assertEqual(new['sessions'][0]['since_id'],'0');self.assertNotIn('provider',new['sessions'][0])
        self.assertTrue(self.execute(ref).replayed)
    def test_input_mutation_does_not_change_retained_packet(self):
        ref=self.prepare();self.rows[0]['title']='changed caller buffer';self.execute(ref)
        self.assertEqual(self.f.current()['sessions'][0]['title'],'retained edit')
    def test_changed_active_or_body_conflicts_with_saved_request(self):
        self.prepare();self.rejects(lambda:self.prepare(active=lists.SID2),'coordination_packet_conflict')
        self.rows[0]['title']='different';self.rejects(self.prepare,'coordination_packet_conflict')
    def test_malformed_original_cas_and_nonmember_active_are_rejected(self):
        self.expected['expected_generation']=True
        self.rejects(self.prepare,'coordination_metadata_packet_invalid')
        self.expected['expected_generation']=0
        self.rejects(lambda:self.prepare(active='missing'),'coordination_metadata_packet_invalid')
    def test_oversized_private_draft_is_refused_without_a_partial_packet(self):
        self.rows[0]['title']='x'*(256*1024)
        self.rejects(self.prepare,'coordination_packet_too_large')
        self.assertEqual(list(self.directory.iterdir()),[]);self.assertEqual(self.f.generation(),0)
    def test_actual_process_exit_reopens_exact_metadata_merge(self):
        ref=self.prepare();code="""import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_publish_metadata_packets import binding,pinned,PrivateSessionMetadataSave,lists
from backend.session_lifecycle_request_packets import PacketReference
a=binding(PrivateSessionMetadataSave,pinned(sys.argv[1]),Path(sys.argv[2]))
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')),patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(127)):
    a.execute(PacketReference(**json.loads(sys.stdin.read())),access=lists.ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,127,p.stderr);a=self.reopen()
        with patch.object(a.commands,'snapshot_sessions',side_effect=AssertionError('no fresh draft')):
            self.assertTrue(self.execute(ref,adapter=a,recover=True).replayed)
        self.assertEqual(self.f.current()['sessions'][0]['title'],'retained edit');self.assertTrue(self.f.current()['sessions'][1]['pinned'])
    def test_pending_recovery_rechecks_native_metadata_row_authorization(self):
        ref=self.prepare();raw=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        a=self.reopen(authorize=lambda _a,_b,sid:sid!=lists.SID2)
        self.rejects(lambda:self.execute(ref,adapter=a,recover=True),'coordination_access_denied')
        self.assertEqual(self.f.h.config_path.read_bytes(),raw)
    def test_after_publication_recovery_and_replay_preserve_newer_edits(self):
        ref=self.prepare();from backend.deployment_config import atomic_write_text
        def published(*args):atomic_write_text(*args);raise SystemExit('synthetic after publication')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=published):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.assertTrue(self.execute(ref,adapter=self.reopen(),recover=True).replayed)
        config_commands(self.a.commands.coordinator).patch_session('later',lists.CID,{'title':'later title'},access=self.access)
        self.assertTrue(self.execute(ref).replayed);self.assertEqual(self.f.current()['sessions'][0]['title'],'later title')
