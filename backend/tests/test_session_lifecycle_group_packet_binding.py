"""Synthetic nested-group packet composition, no HTTP or model dispatch."""
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
from backend.session_lifecycle_group_packet_binding import PrivateApiGroupDelete,CONTRACT
from backend.session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from backend.session_lifecycle_api_group_delete import DELETE
from backend.tests import test_session_lifecycle_api_group_delete as fixtures
from backend.tests.test_session_lifecycle_api_group_delete import CID,A,B,NEXT,ACCESS,STAMP,writer
from backend.tests.test_session_lifecycle_coordination import coordinator


def adapter(w,directory,*,authorize=lambda *_:True,enabled=True):
    c=w.coordinator;binding=digest(encode([str(w.canonical_path),str(c.config),str(c.directory)]))
    p=PrivateCommandPackets(directory,binding=binding,authorize=authorize,enabled=True)
    return PrivateApiGroupDelete(w,p,session_lock=w.session_lock,enabled=enabled)


@unittest.skipUnless(os.name=='posix','private POSIX packet adapter')
class GroupPacketBindingTests(unittest.TestCase):
    def setUp(self):
        # Containment, rather than inheritance, avoids rerunning the old suite.
        self.f=fixtures.ApiGroupDeleteTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.directory=self.f.root/'private-packets';self.directory.mkdir(mode=0o700)
        self.a=adapter(self.f.w,self.directory)
    def prepare(self):return self.a.prepare(CID,request_id='group-packet',deleted_at=STAMP,next_active=NEXT,access=ACCESS)
    def execute(self,ref,**kw):return self.a.execute(CID,ref,access=kw.pop('access',ACCESS),**kw)
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)
    def reopen(self,authorize=lambda *_:True):
        return adapter(writer(coordinator(self.f.h.path),self.f.h.path,authorize=authorize),self.directory)

    def test_default_disabled_and_denied_precede_dependencies(self):
        a=PrivateApiGroupDelete(None,None,session_lock=None)
        self.rejects(lambda:a.prepare(CID,request_id='x',deleted_at=STAMP,access=ACCESS),'lifecycle_disabled')
        a=adapter(writer(self.f.c,self.f.h.path,authorize=lambda *_:False),self.directory)
        a.packets.directory=Path('/missing')
        self.rejects(lambda:a.prepare(CID,request_id='x',deleted_at=STAMP,access=ACCESS),'coordination_access_denied')

    def test_prepare_retains_exact_group_without_sql_effects(self):
        raw=self.f.h.config_path.read_bytes();rows=self.f.rows();ref=self.prepare()
        self.assertEqual(self.prepare(),ref);self.assertEqual(self.f.rows(),rows);self.assertEqual(self.f.h.config_path.read_bytes(),raw)
        self.assertEqual(self.f.count('session_coordination_ops'),0)
        saved=self.a.packets.load(ref.request_id,DELETE,CID,expected_sha=ref.packet_sha,access=ACCESS)
        self.assertEqual(saved['preconditions'],self.f.request);self.assertEqual(saved['contract'],CONTRACT)
        self.assertEqual(saved['preconditions']['expected_group_ids'],sorted([CID,A,B]))
        for private in ('synthetic private','synthetic secret'):self.assertNotIn(private,str(saved))

    def test_prepare_requires_all_member_and_fallback_write_scopes(self):
        for denied in (A,B,NEXT):
            w=writer(self.f.c,self.f.h.path,authorize=lambda _a,action,sid:action!=DELETE or sid!=denied)
            a=adapter(w,self.directory)
            self.rejects(lambda:a.prepare(CID,request_id='x',deleted_at=STAMP,next_active=NEXT,access=ACCESS),'coordination_access_denied')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_same_lock_and_resource_namespace_required_before_packet_io(self):
        self.a.session_lock=RLock();self.rejects(self.prepare,'coordination_lock_mismatch')
        self.a.session_lock=self.f.w.session_lock;self.a.packets.binding='0'*64
        self.rejects(self.prepare,'coordination_resource_mismatch');self.assertEqual(list(self.directory.iterdir()),[])

    def test_packet_store_denial_blocks_prepare_and_execute(self):
        self.a.packets.authorize=lambda *_:False
        self.rejects(self.prepare,'coordination_access_denied');self.a.packets.authorize=lambda *_:True;ref=self.prepare()
        self.a.packets.authorize=lambda *_:False;self.rejects(lambda:self.execute(ref),'coordination_access_denied')
        self.assertEqual(self.f.count('session_coordination_ops'),0)

    def test_execute_uses_saved_packet_and_historical_replay(self):
        ref=self.prepare();outside=[r for r in self.f.rows() if r[0] not in self.f.request['expected_message_ids']]
        with patch.object(self.a.commands,'snapshot_group_delete',side_effect=AssertionError('no new preflight')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(self.f.rows(),outside);self.assertEqual(len(self.f.config()['deleted_sessions']),3)

    def test_new_group_content_after_prepare_loses_original_cas(self):
        ref=self.prepare();self.f.h.conn.execute("UPDATE messages SET text='later body' WHERE id=2")
        self.rejects(lambda:self.execute(ref),'coordination_group_state_changed')
        self.assertEqual(self.f.count('session_coordination_ops'),0)

    def test_original_member_revocation_still_blocks_saved_packet(self):
        ref=self.prepare();a=self.reopen(authorize=lambda _a,_b,sid:sid!=B)
        self.rejects(lambda:a.execute(CID,ref,access=ACCESS),'coordination_access_denied')
        self.assertEqual(self.f.count('session_coordination_ops'),0)

    def test_wrong_actor_reference_digest_or_root_cannot_execute(self):
        ref=self.prepare()
        self.rejects(lambda:self.execute(ref,access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.execute(PacketReference(ref.request_id,'0'*64)),'coordination_packet_changed')
        self.rejects(lambda:self.a.execute(NEXT,ref,access=ACCESS),'coordination_packet_conflict')

    def test_malformed_reference_and_recover_type_refuse(self):
        ref=self.prepare();self.rejects(lambda:self.execute({'request_id':ref.request_id}),'coordination_group_packet_invalid')
        self.rejects(lambda:self.execute(ref,recover=1),'coordination_group_packet_invalid')

    def test_extra_or_missing_packet_fields_cannot_supply_access_or_dispatch(self):
        for addition in ({'access':{'principal_id':'admin'}},{'recover':True},{'method':'anything'}):
            packet={'contract':CONTRACT,'root':CID,'request_id':'forged','deleted_at':STAMP,'next_active':NEXT,
                'preconditions':self.f.request,**addition}
            with patch.object(self.a.packets,'load',return_value=packet):
                self.rejects(lambda:self.execute(PacketReference('forged','0'*64)),'coordination_group_packet_invalid')

    def test_before_publication_process_exit_recovers_exact_original_group(self):
        ref=self.prepare()
        code="""import json,os,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_group_packet_binding import adapter
from backend.tests.test_session_lifecycle_api_group_delete import writer,CID,ACCESS
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.session_lifecycle_request_packets import PacketReference
a=adapter(writer(coordinator(sys.argv[1]),sys.argv[1]),Path(sys.argv[2]))
with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(120)):
    a.execute(CID,PacketReference(**json.loads(sys.stdin.read())),access=ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,120,p.stderr);self.assertEqual(self.f.count('web_message_versions'),0)
        reopened=self.reopen()
        with patch.object(reopened.commands,'snapshot_group_delete',side_effect=AssertionError('deleted graph')):
            self.assertTrue(reopened.execute(CID,ref,access=ACCESS,recover=True).replayed)
        self.assertEqual(self.f.config()['active_session'],NEXT);self.assertEqual(len(self.f.config()['deleted_sessions']),3)

    def test_pending_new_ingress_refuses_recovery_without_reerasing(self):
        ref=self.prepare()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute("INSERT INTO messages VALUES (60,?,'in','user','new private body',?)",(STAMP,json.dumps({'api_session':B})))
        self.rejects(lambda:self.reopen().execute(CID,ref,access=ACCESS,recover=True),'coordination_group_effect_changed')
        self.assertEqual(self.f.h.conn.execute('SELECT text FROM messages WHERE id=60').fetchone()[0],'new private body')

    def test_saved_reference_cannot_cross_resource_installation(self):
        ref=self.prepare();self.a.commands.coordinator.directory=self.f.root/'another-gate'
        self.rejects(lambda:self.execute(ref),'coordination_resource_mismatch')

    def test_empty_recovery_and_post_publication_finalization_do_not_erase_again(self):
        ref=self.prepare();self.rejects(lambda:self.execute(ref,recover=True),'coordination_request_missing')
        from backend.deployment_config import atomic_write_text
        def publish_then_exit(*args):atomic_write_text(*args);raise SystemExit('synthetic after publication')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish_then_exit):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute("INSERT INTO messages VALUES (60,?,'in','user','later bypass input',?)",(STAMP,json.dumps({'api_session':B})))
        self.assertTrue(self.reopen().execute(CID,ref,access=ACCESS,recover=True).replayed)
        self.assertTrue(self.f.h.conn.execute('SELECT 1 FROM messages WHERE id=60').fetchone())

    def test_integration_flags_stay_off(self):
        for flag in ('production_supported','http_integrated','automatic_recovery_supported'):self.assertFalse(getattr(self.a,flag))

    def test_private_file_failure_prevents_any_command_effect(self):
        ref=self.prepare();next(self.directory.glob('*.json')).chmod(0o644)
        self.rejects(lambda:self.execute(ref),'coordination_packet_private_file_required')
        self.assertEqual(self.f.count('session_coordination_ops'),0);self.assertEqual(self.f.count('web_message_versions'),2)

    def test_pending_member_revocation_refuses_publication(self):
        ref=self.prepare();old=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        a=self.reopen(authorize=lambda _a,_b,sid:sid!=A)
        self.rejects(lambda:a.execute(CID,ref,access=ACCESS,recover=True),'coordination_access_denied')
        self.assertEqual(self.f.h.config_path.read_bytes(),old)
