"""Explicit global history setting; private packet and real crash recovery."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from backend.session_lifecycle_history_packet import PrivateHistorySetting, ACTION
from backend.session_lifecycle_request_packets import PacketReference
from backend.tests import test_session_lifecycle_patch_packet as fixtures


def binding(path, directory):
    old=fixtures.binding(path,directory)
    return PrivateHistorySetting(old.commands,old.packets,session_lock=old.session_lock,enabled=True)


@unittest.skipUnless(os.name=='posix','private POSIX packet')
class HistorySettingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PatchPacketTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.a=binding(self.f.f.h.path,self.f.directory);self.access=self.f.access
    def prepare(self,n=73):return self.a.prepare(n,request_id='history-setting',access=self.access)
    def execute(self,r,**kw):return self.a.execute(r,access=self.access,**kw)
    def reject(self,fn,c):self.f.rejects(fn,c)
    def test_prepare_freezes_only_setting_without_io_or_fallback_read(self):
        raw=self.f.f.h.config_path.read_bytes()
        self.a.commands._routes=lambda:self.fail('prepare read credentials')
        r=self.prepare();self.assertEqual(r,self.prepare())
        p=self.a.packets.load(r.request_id,ACTION,None,expected_sha=r.packet_sha,access=self.access)
        self.assertEqual(p['body'],{'history_n':73});self.assertIsNone(p['session_id'])
        self.assertEqual(raw,self.f.f.h.config_path.read_bytes())
    def test_strict_count_range_and_type_before_retention(self):
        for n in (-1,201,True,'7',None,3.5,{'main_chain':[]}):
            self.reject(lambda:self.prepare(n),'coordination_history_packet_invalid')
        self.assertEqual(list(self.f.directory.iterdir()),[])
    def test_execute_and_historical_replay_preserve_later_setting_and_metadata(self):
        r=self.prepare();self.execute(r)
        self.a.commands.update_config('later',{'history_n':11},access=self.access)
        self.a.commands.patch_session('title',self.f.sid,{'title':'later'},access=self.access)
        self.assertTrue(self.execute(r,recover=True).replayed)
        cfg=self.f.f.h.get('legacy');self.assertEqual(cfg['history_n'],11)
        self.assertEqual(cfg['sessions'][0]['title'],'later')
    def test_global_command_and_private_storage_authorization_are_separate(self):
        calls=[];self.a.commands._authorize=lambda a,action,target:calls.append((action,target)) or target is None
        r=self.prepare();self.assertTrue(all(t is None for _,t in calls))
        self.a.packets.authorize=lambda *_:False
        self.reject(lambda:self.execute(r),'coordination_access_denied')
        self.a.packets.authorize=lambda *_:True;self.a.commands._authorize=lambda *_:False
        self.reject(lambda:self.execute(r),'coordination_access_denied')
    def test_default_disabled_and_resource_binding_required(self):
        off=PrivateHistorySetting(None,None,session_lock=None)
        self.reject(lambda:off.execute(PacketReference('x','0'*64),access=self.access),'lifecycle_disabled')
        self.a.packets.binding='0'*64;self.reject(self.prepare,'coordination_resource_mismatch')
    def test_cross_action_and_untrusted_dispatch_cannot_use_setting_packet(self):
        r=self.prepare()
        self.reject(lambda:self.f.a.execute(self.f.sid,r,access=self.access),'coordination_packet_conflict')
        p=self.a.packets.load(r.request_id,ACTION,None,expected_sha=r.packet_sha,access=self.access)
        for value in ({**p,'method':'migrate'},{**p,'body':{'main_chain':[]}},{**p,'request_id':'other'}):
            with patch.object(self.a.packets,'load',return_value=value):
                self.reject(lambda:self.execute(r),'coordination_history_packet_invalid')
    def test_prepared_recover_does_not_start_and_policy_change_refuses_pending(self):
        r=self.prepare();self.reject(lambda:self.execute(r,recover=True),'coordination_request_missing')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            self.reject(lambda:self.execute(r),'coordination_unavailable')
        self.a.commands._routes=lambda:[]
        self.reject(lambda:self.execute(r,recover=True),'coordination_request_conflict')
    def test_real_process_exit_restores_original_setting_without_reapplying_mutator(self):
        r=self.prepare();code='''import os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_history_packet import binding
from backend.tests.test_session_lifecycle_commands import ACCESS
from backend.session_lifecycle_request_packets import PacketReference
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
 a=binding(sys.argv[1],Path(sys.argv[2]))
 with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(129)):
  a.execute(PacketReference(sys.argv[3],sys.argv[4]),access=ACCESS)
'''
        p=subprocess.run([sys.executable,'-c',code,str(self.f.f.h.path),str(self.f.directory),r.request_id,r.packet_sha],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,129,p.stderr)
        self.a=binding(self.f.f.h.path,self.f.directory)
        with patch('backend.session_lifecycle_commands.updated_config',side_effect=AssertionError('no reapply')):
            self.assertTrue(self.execute(r,recover=True).replayed)
        self.assertEqual(self.f.f.h.get('legacy')['history_n'],73)
    def test_explicit_last_writer_has_no_draft_cas_and_preserves_route(self):
        r=self.prepare();self.a.commands.update_config('later',{'history_n':5},access=self.access)
        before=self.f.f.h.get('legacy')['main_chain'];self.execute(r)
        cfg=self.f.f.h.get('legacy');self.assertEqual(cfg['history_n'],73);self.assertEqual(cfg['main_chain'],before)
        self.assertFalse(self.a.draft_cas_supported);self.assertFalse(self.a.http_integrated)


if __name__=='__main__':unittest.main()
