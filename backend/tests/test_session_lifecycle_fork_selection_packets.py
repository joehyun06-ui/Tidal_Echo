"""Offline branch and selector adapters with real original-packet recovery."""
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
from backend.session_lifecycle_fork_selection_packets import PrivateApiFork,PrivateVersionSelection
from backend.session_lifecycle_api_fork import FORK
from backend.session_lifecycle_version_selection import SELECT
from backend.tests import test_session_lifecycle_api_fork as forks
from backend.tests import test_session_lifecycle_version_selection as selections
from backend.tests.test_session_lifecycle_coordination import coordinator


def binding(cls,w,directory,*,authorize=lambda *_:True):
    c=w.coordinator;value=digest(encode([str(w.canonical_path),str(c.config),str(c.directory)]))
    p=PrivateCommandPackets(directory,binding=value,authorize=authorize,enabled=True)
    return cls(w,p,session_lock=w.session_lock,enabled=True)


class _Assertions:
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def execute(self,ref,**kw):return self.a.execute(self.cid,ref,access=kw.pop('access',self.access),**kw)
    def test_default_disabled_and_denied_precede_io(self):
        a=self.cls(None,None,session_lock=None)
        self.rejects(lambda:a.execute(self.cid,PacketReference('x','0'*64),access=self.access),'lifecycle_disabled')
        self.a.commands._authorize=lambda *_:False;self.a.packets.directory=Path('/missing')
        self.rejects(self.prepare,'coordination_access_denied')
    def test_matching_lock_and_namespace_required(self):
        self.a.session_lock=RLock();self.rejects(self.prepare,'coordination_lock_mismatch')
        self.a.session_lock=self.f.w.session_lock;self.a.packets.binding='0'*64
        self.rejects(self.prepare,'coordination_resource_mismatch');self.assertEqual(list(self.directory.iterdir()),[])
    def test_current_actor_root_reference_and_recover_type_bound(self):
        ref=self.prepare()
        self.rejects(lambda:self.execute(ref,access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.execute(PacketReference(ref.request_id,'0'*64)),'coordination_packet_changed')
        self.rejects(lambda:self.a.execute('different',ref,access=self.access),'coordination_packet_conflict')
        self.rejects(lambda:self.execute({'request_id':ref.request_id}),'coordination_api_packet_invalid')
        self.rejects(lambda:self.execute(ref,recover=1),'coordination_api_packet_invalid')
    def test_storage_permission_and_private_mode_failure_block_commands(self):
        ref=self.prepare();self.a.packets.authorize=lambda *_:False
        self.rejects(lambda:self.execute(ref),'coordination_access_denied');self.a.packets.authorize=lambda *_:True
        next(self.directory.glob('*.json')).chmod(0o644)
        self.rejects(lambda:self.execute(ref),'coordination_packet_private_file_required');self.assertEqual(self.f.count('session_coordination_ops'),0)
    def test_prepared_only_recovery_cannot_start_mutation(self):
        ref=self.prepare();self.rejects(lambda:self.execute(ref,recover=True),'coordination_request_missing')
        self.assertEqual(self.f.count('session_coordination_ops'),0)
    def test_packet_fields_cannot_supply_access_recover_or_dispatch(self):
        ref=self.prepare();packet=self.a.packets.load(ref.request_id,self.a.action,self.cid,expected_sha=ref.packet_sha,access=self.access)
        for extra in ({'access':{'principal_id':'admin'}},{'recover':True},{'method':'other'}):
            with patch.object(self.a.packets,'load',return_value={**packet,**extra}):
                self.rejects(lambda:self.execute(ref),'coordination_api_packet_invalid')
        with patch.object(self.a.packets,'load',return_value={**packet,'preconditions':{}}):
            self.rejects(lambda:self.execute(ref),'coordination_api_packet_invalid')
    def test_flags_remain_off(self):
        for flag in ('production_supported','http_integrated','automatic_recovery_supported'):self.assertFalse(getattr(self.a,flag))
    def test_other_action_and_wrong_concrete_dependency_cannot_consume_reference(self):
        ref=self.prepare()
        if self.cls is PrivateApiFork:other_cls,other_writer=PrivateVersionSelection,selections.writer
        else:other_cls,other_writer=PrivateApiFork,forks.writer
        other=binding(other_cls,other_writer(self.f.c,self.f.h.path),self.directory)
        self.rejects(lambda:other.execute(self.cid,ref,access=self.access),'coordination_packet_conflict')
        self.a.commands=other.commands
        self.rejects(lambda:self.execute(ref),'coordination_api_packet_invalid')
        self.assertEqual(self.f.count('session_coordination_ops'),0)


@unittest.skipUnless(os.name=='posix','private POSIX fork packets')
class ForkPacketTests(_Assertions,unittest.TestCase):
    def setUp(self):
        self.f=forks.ApiForkTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.cls=PrivateApiFork;self.cid=forks.CID;self.access=forks.ACCESS
        self.a=binding(self.cls,self.f.w,self.directory)
    def prepare(self,body=None):
        return self.a.prepare(self.cid,forks.BODY if body is None else body,title=forks.TITLE,created_at=forks.STAMP,access=self.access)
    def reopen(self,authorize=lambda *_:True):
        return binding(self.cls,forks.writer(coordinator(self.f.h.path),self.f.h.path,authorize=authorize),self.directory)

    def test_prepare_retains_exact_snapshot_without_canonical_effects(self):
        raw=self.f.h.config_path.read_bytes();rows=self.f.rows(self.cid);schema=self.f.schema();ref=self.prepare()
        self.assertEqual(self.prepare(),ref);self.assertEqual(ref.request_id,forks.TARGET)
        packet=self.a.packets.load(ref.request_id,FORK,self.cid,expected_sha=ref.packet_sha,access=self.access)
        self.assertEqual(packet['preconditions'],self.f.request);self.assertEqual(packet['body'],forks.BODY)
        self.assertEqual(self.f.rows(self.cid),rows);self.assertEqual(self.f.schema(),schema);self.assertEqual(self.f.h.config_path.read_bytes(),raw)
        for private in ('synthetic text','synthetic secret','private usage'):self.assertNotIn(private,str(packet))

    def test_source_and_new_target_write_permission_precede_retention(self):
        for denied in (self.cid,forks.TARGET):
            self.a.commands._authorize=lambda _a,action,sid:action!=FORK or sid!=denied
            self.rejects(self.prepare,'coordination_access_denied')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_fixed_branch_copy_replays_without_snapshot_or_new_active_chat(self):
        ref=self.prepare();active=self.f.config()['active_session']
        with patch.object(self.a.commands,'snapshot_fork',side_effect=AssertionError('no new snapshot')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(len(self.f.rows()),4);self.assertEqual(self.f.config()['active_session'],active)
        self.assertEqual(len(self.f.config()['sessions']),2)

    def test_uuid_alias_normalizes_but_changed_point_conflicts(self):
        ref=self.prepare();self.assertEqual(self.prepare({**forks.BODY,'request_id':forks.RID.upper()}),ref)
        self.rejects(lambda:self.prepare({**forks.BODY,'message_id':2}),'coordination_packet_conflict')

    def test_saved_reference_rechecks_source_and_current_command_authority(self):
        ref=self.prepare();self.f.h.conn.execute("UPDATE messages SET text='changed source' WHERE id=2")
        self.rejects(lambda:self.execute(ref),'coordination_fork_source_changed')
        a=self.reopen(authorize=lambda _a,_b,sid:sid!=forks.TARGET)
        self.rejects(lambda:a.execute(self.cid,ref,access=self.access),'coordination_access_denied')

    def test_reference_target_cannot_be_changed_inside_packet(self):
        ref=self.prepare();p=self.a.packets.load(ref.request_id,FORK,self.cid,expected_sha=ref.packet_sha,access=self.access)
        p['body']={**p['body'],'request_id':'22345678-1234-4234-8234-123456789abc'}
        with patch.object(self.a.packets,'load',return_value=p):self.rejects(lambda:self.execute(ref),'coordination_api_packet_invalid')

    def test_real_process_exit_reopens_original_branch_without_recopy(self):
        ref=self.prepare();code="""import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_fork_selection_packets import binding,PrivateApiFork,forks,coordinator
from backend.session_lifecycle_request_packets import PacketReference
a=binding(PrivateApiFork,forks.writer(coordinator(sys.argv[1]),sys.argv[1]),Path(sys.argv[2]))
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')), patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(122)):
    a.execute(forks.CID,PacketReference(**json.loads(sys.stdin.read())),access=forks.ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,122,p.stderr);copied=self.f.rows();self.assertEqual(len(copied),4)
        self.f.h.conn.execute("UPDATE messages SET text='source changed after intent' WHERE id=2")
        a=self.reopen()
        with patch.object(a.commands,'snapshot_fork',side_effect=AssertionError('no source re-preflight')):
            self.assertTrue(a.execute(self.cid,ref,access=self.access,recover=True).replayed)
        self.assertEqual(self.f.rows(),copied)

    def test_pending_new_branch_input_refuses_recovery_without_erasing_it(self):
        ref=self.prepare();old=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute("INSERT INTO messages VALUES (60,?,'in','user','new input',?)",(forks.STAMP,json.dumps({'api_session':forks.TARGET})))
        self.rejects(lambda:self.reopen().execute(self.cid,ref,access=self.access,recover=True),'coordination_fork_copy_changed')
        self.assertEqual(self.f.h.config_path.read_bytes(),old);self.assertEqual(len(self.f.rows()),5)

    def test_after_publication_preserves_later_branch_history(self):
        ref=self.prepare();from backend.deployment_config import atomic_write_text
        def published(*args):atomic_write_text(*args);raise SystemExit('synthetic after publication')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=published):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute("INSERT INTO messages VALUES (60,?,'in','user','later input',?)",(forks.STAMP,json.dumps({'api_session':forks.TARGET})))
        before=self.f.rows();self.assertTrue(self.reopen().execute(self.cid,ref,access=self.access,recover=True).replayed)
        self.assertEqual(self.f.rows(),before)


@unittest.skipUnless(os.name=='posix','private POSIX selector packets')
class SelectionPacketTests(_Assertions,unittest.TestCase):
    def setUp(self):
        self.f=selections.VersionSelectionTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.cls=PrivateVersionSelection;self.cid=selections.CID;self.access=selections.ACCESS
        self.a=binding(self.cls,self.f.w,self.directory)
    def prepare(self,target=selections.A):
        return self.a.prepare(self.cid,{'version_id':target},request_id='select-packet',access=self.access)
    def reopen(self,authorize=lambda *_:True):
        return binding(self.cls,selections.writer(coordinator(self.f.h.path),self.f.h.path,authorize=authorize),self.directory)

    def test_prepare_captures_full_group_with_no_selection_or_json_change(self):
        raw=self.f.h.config_path.read_bytes();rows=self.f.rows();selected=self.f.selected();ref=self.prepare()
        self.assertEqual(self.prepare(),ref)
        p=self.a.packets.load(ref.request_id,SELECT,self.cid,expected_sha=ref.packet_sha,access=self.access)
        self.assertEqual(p['preconditions'],self.f.request);self.assertEqual(p['request_id'],'select-packet')
        self.assertEqual(self.f.rows(),rows);self.assertEqual(self.f.selected(),selected)
        self.assertEqual(self.f.h.config_path.read_bytes(),raw);self.assertEqual(self.f.count('session_coordination_ops'),0)

    def test_each_original_group_member_requires_current_write_access(self):
        for denied in (self.cid,selections.A,selections.B,selections.C):
            self.a.commands._authorize=lambda _a,action,sid:action!=SELECT or sid!=denied
            self.rejects(self.prepare,'coordination_access_denied')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_selection_is_sql_only_and_saved_reference_replays(self):
        ref=self.prepare();raw=self.f.h.config_path.read_bytes();rows=self.f.rows()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=AssertionError('no JSON publish')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(self.f.selected(),selections.A);self.assertEqual(self.f.rows(),rows);self.assertEqual(self.f.h.config_path.read_bytes(),raw)

    def test_changed_target_cannot_overwrite_same_retained_request(self):
        self.prepare();self.rejects(lambda:self.prepare(selections.B),'coordination_packet_conflict')

    def test_graph_change_after_prepare_loses_cas(self):
        ref=self.prepare();self.f.h.conn.execute('UPDATE web_message_versions SET point_key=point_key+1 WHERE version_id=?',(selections.C,))
        self.rejects(lambda:self.execute(ref),'coordination_cas_conflict');self.assertEqual(self.f.generation(),0)

    def test_saved_packet_still_requires_all_group_authorization(self):
        ref=self.prepare();a=self.reopen(authorize=lambda _a,_b,sid:sid!=selections.C)
        self.rejects(lambda:a.execute(self.cid,ref,access=self.access),'coordination_access_denied')

    def test_packet_request_identity_cannot_be_replaced(self):
        ref=self.prepare();p=self.a.packets.load(ref.request_id,SELECT,self.cid,expected_sha=ref.packet_sha,access=self.access)
        with patch.object(self.a.packets,'load',return_value={**p,'request_id':'different'}):
            self.rejects(lambda:self.execute(ref),'coordination_api_packet_invalid')

    def test_real_process_exit_after_sql_commit_replays_without_restoring_old_choice(self):
        ref=self.prepare();code="""import json,os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_fork_selection_packets import binding,PrivateVersionSelection,selections,coordinator
from backend.session_lifecycle_request_packets import PacketReference
a=binding(PrivateVersionSelection,selections.writer(coordinator(sys.argv[1]),sys.argv[1]),Path(sys.argv[2]))
unlink=Path.unlink
def stop(p,*args,**kwargs):
    if p.name.startswith('config-') and p.suffix=='.stage':os._exit(123)
    return unlink(p,*args,**kwargs)
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')), patch.object(Path,'unlink',new=stop):
    a.execute(selections.CID,PacketReference(**json.loads(sys.stdin.read())),access=selections.ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,123,p.stderr);self.assertEqual(self.f.selected(),selections.A)
        self.f.select(target=selections.B,request=self.f.snapshot(selections.B),rid='later-selection')
        a=self.reopen()
        with patch.object(a.commands,'snapshot_selection',side_effect=AssertionError('no new preflight')):
            self.assertTrue(a.execute(self.cid,ref,access=self.access,recover=True).replayed)
        self.assertEqual(self.f.selected(),selections.B)
