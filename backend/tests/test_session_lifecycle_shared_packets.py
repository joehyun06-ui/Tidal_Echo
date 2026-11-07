"""Four fixed adapters on one real lock/gate/DB/config/private namespace."""
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock,Barrier
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
from backend.session_lifecycle import LifecycleError
from backend.tests.test_session_lifecycle import CID
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_patch_packet import PrivateSessionPatch
from backend.session_lifecycle_history_packet import PrivateHistorySetting
from backend.session_lifecycle_model_packets import PrivateModelMigration,PrivateModelRollback
from backend.session_lifecycle_request_packets import PrivateCommandPackets
from backend.session_lifecycle_coordination import digest,encode
from backend.tests import test_session_lifecycle_model_commands as fixtures
from backend.tests.test_session_lifecycle_commands import commands
from backend.tests.test_session_lifecycle_create_delete_packets import pinned


def bundle(path,directory,authorize=lambda *_:True):
    c=pinned(path);lock=RLock()
    config=commands(c,lock=lock,authorize=authorize)
    model=fixtures.model_commands(c,authorize=authorize);model.session_lock=lock
    store=PrivateCommandPackets(directory,binding=digest(encode([str(c.canonical_path),str(c.config),str(c.directory)])),
        authorize=lambda *_:True,enabled=True)
    return tuple(cls(w,store,session_lock=lock,enabled=True) for cls,w in (
        (PrivateSessionPatch,config),(PrivateHistorySetting,config),(PrivateModelMigration,model),(PrivateModelRollback,model)))


@unittest.skipUnless(os.name=='posix','private POSIX integration')
class SharedPacketIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.p,self.h,self.m,self.u=bundle(self.f.h.path,self.directory);self.access=fixtures.ACCESS
    def refs(self):
        return (self.p.prepare(CID,{'title':'shared title','pinned':True},request_id='patch',access=self.access),
            self.h.prepare(77,request_id='history',access=self.access),
            self.m.prepare(fixtures.NEW,request_id='migrate',expected_model=fixtures.OLD,expected_generation=0,access=self.access),
            self.u.prepare(fixtures.rollback_payload(),request_id='undo',access=self.access))
    def run_ref(self,a,r,recover=False):
        if a is self.p:return a.execute(CID,r,access=self.access,recover=recover)
        return a.execute(r,access=self.access,recover=recover)
    def reject(self,fn,c):self.f.rejects(fn,c)
    def test_four_paths_share_actual_lock_store_and_coordinator(self):
        values=(self.p,self.h,self.m,self.u)
        self.assertTrue(all(a.session_lock is self.p.session_lock and a.packets is self.p.packets
            and a.commands.coordinator is self.p.commands.coordinator for a in values))
        before=self.f.h.config_path.read_bytes();self.refs()
        self.assertEqual(before,self.f.h.config_path.read_bytes());self.assertEqual(self.f.generation(),0)
    def test_all_prepared_before_execution_keep_original_model_cas(self):
        p,h,m,u=self.refs();self.run_ref(self.p,p);self.run_ref(self.h,h)
        before=self.f.h.config_path.read_bytes()
        self.reject(lambda:self.run_ref(self.m,m),'coordination_cas_conflict')
        self.reject(lambda:self.run_ref(self.u,u),'coordination_cas_conflict')
        self.assertEqual(before,self.f.h.config_path.read_bytes());self.assertEqual(self.f.generation(),2)
    def test_model_then_genuine_undo_then_metadata_preserves_every_field(self):
        p,h,m,u=self.refs();self.run_ref(self.m,m);self.run_ref(self.u,u)
        self.run_ref(self.p,p);self.run_ref(self.h,h)
        cfg=self.f.h.get('legacy');self.assertEqual(cfg['main_chain'],[fixtures.ROUTE])
        self.assertEqual(cfg['sessions'][0]['title'],'shared title');self.assertTrue(cfg['sessions'][0]['pinned'])
        self.assertEqual(cfg['history_n'],77);self.assertEqual(self.f.generation(),4)
        for a,r in zip((self.p,self.h,self.m,self.u),(p,h,m,u)):self.assertTrue(self.run_ref(a,r).replayed)
        self.assertEqual(cfg,self.f.h.get('legacy'));self.assertEqual(self.f.generation(),4)
    def test_later_metadata_blocks_old_undo_without_reverting_other_fields(self):
        p,h,m,u=self.refs();self.run_ref(self.m,m);self.run_ref(self.p,p)
        self.reject(lambda:self.run_ref(self.u,u),'coordination_cas_conflict')
        cfg=self.f.h.get('legacy');self.assertEqual(cfg['main_chain'][0]['model'],fixtures.NEW)
        self.assertEqual(cfg['sessions'][0]['title'],'shared title')
    def test_request_id_collision_between_actions_is_not_separate_success(self):
        self.p.prepare(CID,{'pinned':True},request_id='collision',access=self.access)
        self.reject(lambda:self.h.prepare(5,request_id='collision',access=self.access),'coordination_packet_conflict')
        self.reject(lambda:self.m.prepare(fixtures.NEW,request_id='collision',expected_model=fixtures.OLD,
            expected_generation=0,access=self.access),'coordination_packet_conflict')
    def test_each_reference_is_refused_by_every_other_adapter(self):
        adapters=(self.p,self.h,self.m,self.u);refs=self.refs()
        for i,ref in enumerate(refs):
            for j,a in enumerate(adapters):
                if i!=j:self.reject(lambda:self.run_ref(a,ref),'coordination_packet_conflict')
    def test_reopened_bundle_cannot_inherit_previous_actor_authority(self):
        p,h,m,u=self.refs();self.run_ref(self.m,m)
        self.p,self.h,self.m,self.u=bundle(self.f.h.path,self.directory,authorize=lambda a,*_:a.principal_id=='other')
        self.reject(lambda:self.run_ref(self.u,u),'coordination_access_denied')
        self.reject(lambda:self.run_ref(self.m,m),'coordination_access_denied')
        self.assertEqual(self.f.generation(),1)
    def test_concurrent_metadata_paths_merge_without_lost_update(self):
        p,h,_,_=self.refs();barrier=Barrier(2)
        def work(a,r):barrier.wait(timeout=5);return self.run_ref(a,r)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(work,self.p,p);b=pool.submit(work,self.h,h)
            self.assertEqual(sorted([a.result(timeout=10).generation,b.result(timeout=10).generation]),[1,2])
        cfg=self.f.h.get('legacy');self.assertEqual(cfg['history_n'],77)
        self.assertEqual(cfg['sessions'][0]['title'],'shared title')
    def test_concurrent_original_generation_model_changes_have_one_winner(self):
        _,_,one,_=self.refs();two=self.m.prepare('second model',request_id='second',expected_model=fixtures.OLD,
            expected_generation=0,access=self.access);barrier=Barrier(2)
        def work(ref):
            barrier.wait(timeout=5)
            try:self.run_ref(self.m,ref);return 'success'
            except LifecycleError as e:return e.category
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(work,[one,two]))
        self.assertEqual(sorted(result),['coordination_cas_conflict','success']);self.assertEqual(self.f.generation(),1)
    def test_actual_crash_blocks_other_writer_until_original_recovery(self):
        p,h,m,u=self.refs();code='''import os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_shared_packets import bundle,fixtures
from backend.session_lifecycle_request_packets import PacketReference
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
 p,h,m,u=bundle(sys.argv[1],Path(sys.argv[2]))
 with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(131)):
  m.execute(PacketReference(sys.argv[3],sys.argv[4]),access=fixtures.ACCESS)
'''
        child=subprocess.run([sys.executable,'-c',code,str(self.f.h.path),str(self.directory),m.request_id,m.packet_sha],capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,131,child.stderr)
        self.p,self.h,self.m,self.u=bundle(self.f.h.path,self.directory)
        self.reject(lambda:self.run_ref(self.h,h),'coordination_recovery_required')
        with patch('backend.session_lifecycle_model_commands.prepare_model_only_change',side_effect=AssertionError('reapplied')):
            self.assertTrue(self.run_ref(self.m,m,True).replayed)
        self.run_ref(self.h,h)
        self.assertEqual(self.f.h.get('legacy')['history_n'],77)
        self.reject(lambda:self.run_ref(self.u,u),'coordination_cas_conflict')


if __name__=='__main__':unittest.main()
