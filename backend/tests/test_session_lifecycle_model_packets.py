"""Original model CAS and receipt-bound compensation, no model network IO."""
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock
import unittest
from unittest.mock import patch
from backend.session_lifecycle_model_packets import PrivateModelMigration,PrivateModelRollback
from backend.session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from backend.session_lifecycle_coordination import digest,encode
from backend.tests import test_session_lifecycle_model_commands as fixtures
from backend.tests.test_session_lifecycle_create_delete_packets import pinned


def binding(cls,path,directory):
    c=pinned(path);w=fixtures.model_commands(c)
    p=PrivateCommandPackets(directory,binding=digest(encode([str(c.canonical_path),str(c.config),str(c.directory)])),
        authorize=lambda *_:True,enabled=True)
    return cls(w,p,session_lock=w.session_lock,enabled=True)


@unittest.skipUnless(os.name=='posix','private POSIX packets')
class ModelPacketTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.m=self.reopen(PrivateModelMigration);self.u=self.reopen(PrivateModelRollback)
    def reopen(self,cls):return binding(cls,self.f.h.path,self.directory)
    def prepare(self):return self.m.prepare(fixtures.NEW,request_id='migrate',expected_model=fixtures.OLD,expected_generation=0,access=fixtures.ACCESS)
    def undo(self,payload=None):return self.u.prepare(fixtures.rollback_payload() if payload is None else payload,request_id='undo',access=fixtures.ACCESS)
    def execute(self,a,r,recover=False):return a.execute(r,access=fixtures.ACCESS,recover=recover)
    def reject(self,fn,c):self.f.rejects(fn,c)
    def test_prepare_is_no_effect_and_retains_no_routes_or_secrets(self):
        raw=self.f.h.config_path.read_bytes();r=self.prepare();self.assertEqual(r,self.prepare());self.undo()
        self.assertEqual(self.f.generation(),0);self.assertEqual(raw,self.f.h.config_path.read_bytes())
        for path in self.directory.glob('*.json'):
            self.assertNotIn(fixtures.ROUTE['key'],path.read_text());self.assertNotIn(fixtures.ROUTE['url'],path.read_text())
    def test_migration_and_genuine_rollback_replay_preserve_original_config(self):
        before=self.f.h.get('legacy');r=self.prepare();self.execute(self.m,r);u=self.undo();self.execute(self.u,u)
        self.assertEqual(self.f.h.get('legacy'),before)
        self.assertTrue(self.execute(self.m,r).replayed);self.assertTrue(self.execute(self.u,u).replayed)
        self.assertEqual(self.f.h.get('legacy'),before)
    def test_original_generation_is_not_refreshed_for_prepared_migration(self):
        r=self.prepare();self.f.migrate(rid='other')
        self.reject(lambda:self.execute(self.m,r),'coordination_cas_conflict')
    def test_rollback_requires_genuine_source_and_current_generation(self):
        u=self.undo();self.reject(lambda:self.execute(self.u,u),'coordination_cas_conflict')
        self.execute(self.m,self.prepare())
        self.f.writer.coordinator=self.m.commands.coordinator
        from backend.tests.test_session_lifecycle_commands import commands
        commands(self.m.commands.coordinator).update_config('later',{'history_n':7},access=fixtures.ACCESS)
        self.reject(lambda:self.execute(self.u,u),'coordination_cas_conflict')
    def test_forged_previous_model_does_not_gain_undo_authority(self):
        self.execute(self.m,self.prepare());p=fixtures.rollback_payload();p['previous_model']='forged'
        u=self.undo(p);self.reject(lambda:self.execute(self.u,u),'coordination_operation_conflict')
    def test_default_off_current_acl_private_acl_and_same_lock_required(self):
        for cls in (PrivateModelMigration,PrivateModelRollback):
            a=cls(None,None,session_lock=None)
            self.reject(lambda:self.execute(a,PacketReference('x','0'*64)),'lifecycle_disabled')
        r=self.prepare();self.m.commands._authorize=lambda *_:False
        self.reject(lambda:self.execute(self.m,r),'coordination_access_denied');self.m=self.reopen(PrivateModelMigration)
        self.m.packets.authorize=lambda *_:False;self.reject(lambda:self.execute(self.m,r),'coordination_access_denied')
        self.m=self.reopen(PrivateModelMigration);self.m.session_lock=RLock()
        self.reject(self.prepare,'coordination_lock_mismatch')
    def test_reference_actor_action_shape_and_mode_are_bound(self):
        r=self.prepare();self.reject(lambda:self.execute(self.u,r),'coordination_packet_conflict')
        self.reject(lambda:self.execute(self.m,PacketReference(r.request_id,'0'*64)),'coordination_packet_changed')
        p=self.m.packets.load(r.request_id,self.m.action,None,expected_sha=r.packet_sha,access=fixtures.ACCESS)
        for extra in ({'method':'other'},{'access':{}},{'recover':True},{'request_id':'other'}):
            with patch.object(self.m.packets,'load',return_value={**p,**extra}):
                self.reject(lambda:self.execute(self.m,r),'coordination_model_packet_invalid')
        next(self.directory.glob('*.json')).chmod(0o644)
        self.reject(lambda:self.execute(self.m,r),'coordination_packet_private_file_required')
    def test_prepared_only_recover_and_resource_mismatch_refuse(self):
        r=self.prepare();u=self.undo()
        for a,ref in ((self.m,r),(self.u,u)):
            self.reject(lambda:self.execute(a,ref,True),'coordination_request_missing')
        self.m.commands.coordinator.canonical_path=None;self.reject(self.prepare,'coordination_resource_mismatch')
    def test_pending_undo_revalidates_source_receipt(self):
        self.execute(self.m,self.prepare());u=self.undo()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.reject(lambda:self.execute(self.u,u),'coordination_unavailable')
        self.f.h.conn.execute("DELETE FROM session_coordination_ops WHERE request_id='migrate'")
        self.reject(lambda:self.execute(self.u,u,True),'coordination_operation_conflict')
    def test_real_process_crash_reopens_both_original_packets_without_reapplying(self):
        r=self.prepare();u=self.undo()
        for cls,ref in ((PrivateModelMigration,r),(PrivateModelRollback,u)):
            code='''import os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_model_packets import binding,PrivateModelMigration,PrivateModelRollback,fixtures
from backend.session_lifecycle_request_packets import PacketReference
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
 a=binding(globals()[sys.argv[1]],sys.argv[2],Path(sys.argv[3]))
 with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(130)):
  a.execute(PacketReference(sys.argv[4],sys.argv[5]),access=fixtures.ACCESS)
'''
            p=subprocess.run([sys.executable,'-c',code,cls.__name__,str(self.f.h.path),str(self.directory),ref.request_id,ref.packet_sha],capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,130,p.stderr)
            a=self.reopen(cls)
            with patch('backend.session_lifecycle_model_commands.prepare_model_only_change',side_effect=AssertionError('reapplied')):
                self.assertTrue(self.execute(a,ref,True).replayed)
        self.assertEqual(self.f.h.get('legacy')['main_chain'][0]['model'],fixtures.OLD)


if __name__=='__main__':unittest.main()
