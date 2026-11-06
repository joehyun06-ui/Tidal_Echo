"""Offline fixed patch packets using real command/coordinator and subprocesses."""
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_coordination import digest, encode
from backend.session_lifecycle_patch_packet import PrivateSessionPatch, ACTION
from backend.session_lifecycle_request_packets import PrivateCommandPackets, PacketReference
from backend.web_session_provider_authority import WebSessionProviderAuthority, WebSessionProviderAuthorityError
from backend.tests import test_session_lifecycle_commands as fixtures
from backend.tests.test_session_lifecycle_create_delete_packets import pinned


def binding(path, directory, authorize=lambda *_: True):
    c = pinned(path)
    w = fixtures.commands(c, authorize=authorize)
    value = digest(encode([str(c.canonical_path), str(c.config), str(c.directory)]))
    store = PrivateCommandPackets(directory, binding=value, authorize=lambda *_: True, enabled=True)
    return PrivateSessionPatch(w, store, session_lock=w.session_lock, enabled=True)


@unittest.skipUnless(os.name == 'posix', 'POSIX private packets')
class PatchPacketTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ConfigCommandTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.directory = self.f.root/'packets'; self.directory.mkdir(mode=0o700)
        self.a = self.reopen(); self.sid = fixtures.CID; self.access = fixtures.ACCESS

    def reopen(self, authorize=lambda *_: True): return binding(self.f.h.path, self.directory, authorize)
    def prepare(self, body=None, rid='patch-packet'):
        return self.a.prepare(self.sid, {'title':'packet title', 'pinned':True} if body is None else body,
            request_id=rid, access=self.access)
    def execute(self, ref, **kw): return self.a.execute(self.sid, ref, access=kw.pop('access', self.access), **kw)
    def rejects(self, fn, category):
        with self.assertRaises(LifecycleError) as caught: fn()
        self.assertEqual(caught.exception.category, category); self.assertEqual(str(caught.exception), category)
    def pending(self, ref):
        with patch('backend.session_lifecycle_coordination.atomic_write_text', side_effect=OSError('private')):
            self.rejects(lambda: self.execute(ref), 'coordination_unavailable')

    def test_prepare_is_idempotent_without_canonical_or_config_reads(self):
        raw = self.f.h.config_path.read_bytes()
        with patch.object(self.a.commands.coordinator, 'fence', side_effect=AssertionError('no snapshot')):
            ref = self.prepare(); self.assertEqual(self.prepare(), ref)
        self.assertEqual(self.f.h.config_path.read_bytes(), raw)
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0], 0)
        p = self.a.packets.load(ref.request_id, ACTION, self.sid, expected_sha=ref.packet_sha, access=self.access)
        self.assertEqual(p['body'], {'title':'packet title', 'pinned':True})
        self.assertNotIn('synthetic secret', str(p)); self.assertNotIn('synthetic secret', repr(ref))

    def test_exact_original_patch_executes_once_and_replays(self):
        ref = self.prepare(); self.assertFalse(self.execute(ref).replayed)
        with patch.object(WebSessionProviderAuthority, 'patch_session', side_effect=AssertionError('rerun')):
            self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(self.f.h.get('legacy')['sessions'][0]['title'], 'packet title')

    def test_history_replay_keeps_later_title_and_settings(self):
        ref = self.prepare(); self.execute(ref)
        self.a.commands.patch_session('later', self.sid, {'title':'later'}, access=self.access)
        self.a.commands.update_config('settings', {'history_n':71}, access=self.access)
        self.assertTrue(self.execute(ref, recover=True).replayed)
        config = self.f.h.get('legacy'); self.assertEqual(config['sessions'][0]['title'], 'later')
        self.assertEqual(config['history_n'], 71)

    def test_patch_only_applies_supplied_fields_without_claiming_draft_cas(self):
        ref = self.prepare({'pinned':True, 'active':False})
        self.a.commands.patch_session('later', self.sid, {'title':'newer'}, access=self.access)
        self.execute(ref); config = self.f.h.get('legacy')
        self.assertEqual(config['sessions'][0]['title'], 'newer'); self.assertTrue(config['sessions'][0]['pinned'])
        self.assertFalse(self.a.draft_cas_supported)

    def test_same_field_patch_is_explicit_last_writer_not_old_draft_protection(self):
        ref = self.prepare({'title':'explicit'})
        self.a.commands.patch_session('later', self.sid, {'title':'newer'}, access=self.access)
        self.execute(ref); self.assertEqual(self.f.h.get('legacy')['sessions'][0]['title'], 'explicit')

    def test_default_off_and_command_disabled_precede_dependencies(self):
        off = PrivateSessionPatch(None, None, session_lock=None)
        self.rejects(lambda: off.execute(self.sid, PacketReference('x','0'*64), access=self.access), 'lifecycle_disabled')
        self.a.commands.enabled=False; self.a.packets=None
        self.rejects(self.prepare, 'coordination_patch_packet_invalid')
        self.a=self.reopen(); self.a.commands.enabled=False
        self.rejects(self.prepare, 'lifecycle_disabled')

    def test_command_denial_precedes_resource_and_storage_reads(self):
        self.a.commands._authorize=lambda *_:False; self.a.commands.coordinator=None
        self.rejects(self.prepare,'coordination_access_denied')

    def test_concrete_dependencies_lock_namespace_and_pin_required(self):
        original=self.a.commands; self.a.commands=object()
        self.rejects(self.prepare,'coordination_patch_packet_invalid'); self.a.commands=original
        self.a.session_lock=RLock(); self.rejects(self.prepare,'coordination_lock_mismatch')
        self.a=self.reopen(); self.a.packets.binding='0'*64
        self.rejects(self.prepare,'coordination_resource_mismatch')
        self.a=self.reopen(); self.a.commands.coordinator.canonical_path=None
        self.rejects(self.prepare,'coordination_resource_mismatch')

    def test_prepare_rejects_nonpatch_fields_and_boolean_coercion(self):
        for body in ({}, [], {'provider':'codex'}, {'method':'delete'}, {'pinned':1}, {'active':'true'}, {'title':None}):
            with self.subTest(body=body): self.rejects(lambda:self.prepare(body),'coordination_patch_packet_invalid')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_input_buffer_is_frozen_and_same_request_cannot_be_replaced(self):
        body={'title':'original'}; ref=self.prepare(body); body['title']='changed'
        self.rejects(lambda:self.prepare(body),'coordination_packet_conflict')
        self.execute(ref); self.assertEqual(self.f.h.get('legacy')['sessions'][0]['title'],'original')

    def test_actor_target_digest_and_action_bound(self):
        ref=self.prepare()
        self.rejects(lambda:self.execute(ref,access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.a.execute('other',ref,access=self.access),'coordination_packet_conflict')
        self.rejects(lambda:self.execute(PacketReference(ref.request_id,'0'*64)),'coordination_packet_changed')
        self.rejects(lambda:self.a.packets.load(ref.request_id,'loop.update.v1',None,expected_sha=ref.packet_sha,access=self.access),
            'coordination_packet_conflict')

    def test_outer_envelope_cannot_inject_auth_dispatch_or_recover(self):
        ref=self.prepare(); p=self.a.packets.load(ref.request_id,ACTION,self.sid,expected_sha=ref.packet_sha,access=self.access)
        for extra in ({'access':{}},{'recover':True},{'method':'update_config'},{'request_id':'other'}, {'contract':'other'}):
            with patch.object(self.a.packets,'load',return_value={**p,**extra}):
                self.rejects(lambda:self.execute(ref),'coordination_patch_packet_invalid')
        self.rejects(lambda:self.execute(ref,recover=1),'coordination_patch_packet_invalid')

    def test_inner_body_is_revalidated_before_fixed_dispatch(self):
        ref=self.prepare(); p=self.a.packets.load(ref.request_id,ACTION,self.sid,expected_sha=ref.packet_sha,access=self.access)
        with patch.object(self.a.packets,'load',return_value={**p,'body':{'history_n':3}}):
            self.rejects(lambda:self.execute(ref),'coordination_patch_packet_invalid')

    def test_storage_permission_and_private_file_mode_are_required(self):
        ref=self.prepare(); self.a.packets.authorize=lambda *_:False
        self.rejects(lambda:self.execute(ref),'coordination_access_denied'); self.a.packets.authorize=lambda *_:True
        next(self.directory.glob('*.json')).chmod(0o644)
        self.rejects(lambda:self.execute(ref),'coordination_packet_private_file_required')

    def test_prepared_only_recovery_never_starts_mutation(self):
        ref=self.prepare(); self.rejects(lambda:self.execute(ref,recover=True),'coordination_request_missing')
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_pending_recovery_rechecks_current_permission_and_does_not_repatch(self):
        ref=self.prepare(); self.pending(ref); self.a=self.reopen(lambda *_:False)
        self.rejects(lambda:self.execute(ref,recover=True),'coordination_access_denied')
        self.a=self.reopen()
        with patch.object(WebSessionProviderAuthority,'patch_session',side_effect=AssertionError('repatch')):
            self.assertTrue(self.execute(ref,recover=True).replayed)

    def test_missing_target_refuses_at_execution_not_prepare(self):
        self.sid='missing'; ref=self.prepare()
        with self.assertRaises(WebSessionProviderAuthorityError):self.execute(ref)
        self.assertEqual(self.f.h.conn.execute('SELECT count(*) FROM session_coordination_ops').fetchone()[0],0)

    def test_real_process_death_after_intent_reopens_original_packet(self):
        ref=self.prepare(); code='''import os,socket,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_patch_packet import binding,fixtures
from backend.session_lifecycle_request_packets import PacketReference
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline')):
 a=binding(sys.argv[1],Path(sys.argv[2]))
 with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(128)):
  a.execute(fixtures.CID,PacketReference(sys.argv[3],sys.argv[4]),access=fixtures.ACCESS)
'''
        p=subprocess.run([sys.executable,'-c',code,str(self.f.h.path),str(self.directory),ref.request_id,ref.packet_sha],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,128,p.stderr); self.a=self.reopen()
        self.assertTrue(self.execute(ref,recover=True).replayed)
        self.assertEqual(self.f.h.get('legacy')['sessions'][0]['title'],'packet title')

    def test_canonical_actual_connection_must_match_named_pin(self):
        self.a.commands.coordinator.canonical_path=self.f.root/'other.sqlite'
        c=self.a.commands.coordinator
        self.a.packets.binding=digest(encode([str(c.canonical_path),str(c.config),str(c.directory)]))
        ref=self.prepare(); self.rejects(lambda:self.execute(ref),'coordination_resource_mismatch')

    def test_metadata_patch_can_keep_codex_provider_without_native_runtime(self):
        config=self.f.h.get('legacy'); config['sessions'][0]['provider']='codex'
        # Explicit synthetic setup through the cooperative fixture coordinator.
        self.a.commands.coordinator.mutate('fixture-provider', {'fixture':True}, lambda legacy:legacy.save_config(config))
        ref=self.prepare(); self.execute(ref)
        self.assertEqual(self.f.h.get('legacy')['sessions'][0]['provider'],'codex')
        self.assertFalse(self.a.native_integrated)


if __name__=='__main__':unittest.main()
