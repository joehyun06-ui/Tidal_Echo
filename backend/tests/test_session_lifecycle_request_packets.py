"""Private packet durability and refusal tests; no live configuration or IO."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_request_packets import PrivateCommandPackets,MAX_BYTES

ACCESS=ConfigCommandAccess('synthetic_owner')
ACTION='session.patch.v1'
TARGET='api-synthetic'


@unittest.skipUnless(os.name=='posix','private POSIX packet candidate')
class PacketTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.directory=self.root/'packets';self.directory.mkdir(mode=0o700)
        self.store=self.writer();self.body={'body':{'title':'synthetic private title'},'expected_generation':3}
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline')))
    def writer(self,**kwargs):
        return PrivateCommandPackets(self.directory,binding='a'*64,authorize=kwargs.get('authorize',lambda *_:True),enabled=kwargs.get('enabled',True))
    def prepare(self,**kwargs):return self.store.prepare('request-1',ACTION,TARGET,self.body,access=kwargs.get('access',ACCESS))
    def load(self,ref,**kwargs):return self.store.load('request-1',kwargs.get('action',ACTION),kwargs.get('target',TARGET),expected_sha=kwargs.get('sha',ref.packet_sha),access=kwargs.get('access',ACCESS))
    def file(self):return next(self.directory.glob('*.json'))
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)

    def test_default_disabled_constructor_has_no_io(self):
        s=PrivateCommandPackets('/missing',binding='bad',authorize=None)
        self.rejects(lambda:s.prepare('x',ACTION,TARGET,{},access=ACCESS),'lifecycle_disabled')

    def test_denied_or_nonboolean_authorization_precedes_io(self):
        for allowed in (False,1,None,'yes'):
            s=self.writer(authorize=lambda *_:allowed);s.directory=Path('/missing')
            self.rejects(lambda:s.prepare('x',ACTION,TARGET,{},access=ACCESS),'coordination_access_denied')

    def test_roundtrip_is_private_fixed_and_content_free_receipt(self):
        ref=self.prepare();self.assertEqual(self.load(ref),self.body)
        self.assertEqual(set(asdict(ref)),{'request_id','packet_sha'});self.assertNotIn('private',repr(ref))
        self.assertEqual(self.file().stat().st_mode&0o777,0o600)
        self.assertNotIn('request-1',self.file().name);self.assertNotIn(TARGET,self.file().name)

    def test_identical_prepare_is_immutable_and_idempotent(self):
        ref=self.prepare();before=self.file().stat().st_ino
        self.assertEqual(self.prepare(),ref);self.assertEqual(self.file().stat().st_ino,before)

    def test_argument_mutation_after_prepare_cannot_change_saved_packet(self):
        ref=self.prepare();self.body['body']['title']='changed'
        self.assertEqual(self.load(ref)['body']['title'],'synthetic private title')
        self.rejects(self.prepare,'coordination_packet_conflict')

    def test_same_request_changed_actor_action_or_target_cannot_overwrite(self):
        ref=self.prepare();raw=self.file().read_bytes()
        self.rejects(lambda:self.prepare(access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.store.prepare('request-1','loop.update.v1',None,{},access=ACCESS),'coordination_packet_conflict')
        self.rejects(lambda:self.load(ref,target='different'),'coordination_packet_conflict')
        self.assertEqual(self.file().read_bytes(),raw)

    def test_binding_separates_independent_namespaces(self):
        self.prepare();other=PrivateCommandPackets(self.directory,binding='b'*64,authorize=lambda *_:True,enabled=True)
        other.prepare('request-1',ACTION,TARGET,{'different':True},access=ACCESS)
        self.assertEqual(len(list(self.directory.glob('*.json'))),2)

    def test_load_missing_does_not_allocate_packet_or_directory(self):
        self.rejects(lambda:self.store.load('request-1',ACTION,TARGET,expected_sha='0'*64,access=ACCESS),'coordination_packet_missing')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_corruption_and_wrong_expected_digest_refuse(self):
        ref=self.prepare();self.rejects(lambda:self.load(ref,sha='0'*64),'coordination_packet_changed')
        self.file().write_text('{}');self.rejects(lambda:self.load(ref),'coordination_packet_changed')

    def test_matching_digest_does_not_override_principal_or_schema(self):
        ref=self.prepare();value=json.loads(self.file().read_text());value['extra']='not permitted'
        raw=json.dumps(value).encode();self.file().write_bytes(raw)
        self.rejects(lambda:self.load(ref,sha=hashlib.sha256(raw).hexdigest()),'coordination_packet_conflict')

    def test_read_rechecks_current_authorization_before_returning_private_body(self):
        ref=self.prepare();n=[]
        self.store.authorize=lambda *_:n.append(1) is None and len(n)==1
        self.rejects(lambda:self.load(ref),'coordination_access_denied')

    def test_prepare_revocation_before_publication_removes_private_stage(self):
        n=[];self.store.authorize=lambda *_:n.append(1) is None and len(n)==1
        self.rejects(self.prepare,'coordination_access_denied');self.assertEqual(list(self.directory.iterdir()),[])

    def test_private_directory_and_file_modes_are_required(self):
        self.directory.chmod(0o755);self.rejects(self.prepare,'coordination_packet_private_directory_required')
        self.directory.chmod(0o700);ref=self.prepare();self.file().chmod(0o644)
        self.rejects(lambda:self.load(ref),'coordination_packet_private_file_required')

    def test_symlinks_directories_fifo_and_hardlinks_refuse(self):
        ref=self.prepare();p=self.file();raw=p.read_bytes();p.unlink()
        other=self.root/'other';other.write_bytes(raw);other.chmod(0o600);p.symlink_to(other)
        self.rejects(lambda:self.load(ref),'coordination_packet_unavailable');p.unlink()
        os.link(other,p);self.rejects(lambda:self.load(ref),'coordination_packet_private_file_required');p.unlink()
        os.mkfifo(p,0o600);self.rejects(lambda:self.load(ref),'coordination_packet_private_file_required');p.unlink()
        p.mkdir();self.rejects(lambda:self.load(ref),'coordination_packet_unavailable')

    def test_directory_symlink_or_relative_path_refuses(self):
        link=self.root/'alias';link.symlink_to(self.directory);self.store.directory=link
        self.rejects(self.prepare,'coordination_packet_invalid');self.store.directory=Path('relative')
        self.rejects(self.prepare,'coordination_packet_invalid')

    def test_payload_bounds_and_strict_json(self):
        for value in ({'large':'x'*MAX_BYTES},{'bad':float('nan')},{'bad':object()}):
            self.body=value
            self.rejects(self.prepare,'coordination_packet_too_large' if 'large' in value else 'coordination_invalid')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_interrupted_before_link_leaves_no_visible_packet(self):
        with patch('backend.session_lifecycle_request_packets.os.link',side_effect=OSError('synthetic failure')):
            self.rejects(self.prepare,'coordination_packet_unavailable')
        self.assertEqual(list(self.directory.iterdir()),[]);self.prepare()

    def test_uncertain_directory_fsync_retains_packet_and_retry_reasserts_durability(self):
        original=os.fsync;calls=[]
        def fail_second(fd):
            calls.append(fd)
            if len(calls)==2:raise OSError('synthetic directory failure')
            original(fd)
        with patch('backend.session_lifecycle_request_packets.os.fsync',side_effect=fail_second):
            self.rejects(self.prepare,'coordination_packet_unavailable')
        self.assertEqual(len(list(self.directory.glob('*.json'))),1);self.assertEqual(self.load(self.prepare()),self.body)

    def test_restart_recovers_actual_coordinator_using_retained_original_arguments(self):
        from backend.tests.test_session_lifecycle_storage import FileHarness
        from backend.tests.test_session_lifecycle_coordination import coordinator
        from backend.tests.test_session_lifecycle_commands import commands
        from backend.tests.test_session_lifecycle import CID
        h=FileHarness(self.root/'relay.sqlite');self.addCleanup(h.conn.close)
        (self.root/'gate').mkdir(mode=0o700);c=coordinator(h.path);c.bootstrap()
        arguments={'request_id':'config-packet','session_id':CID,'body':{'title':'recovered synthetic title'}}
        ref=self.store.prepare('config-packet',ACTION,CID,arguments,access=ACCESS)
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):commands(c).patch_session(access=ACCESS,**arguments)
        reopened=self.writer();saved=reopened.load('config-packet',ACTION,CID,expected_sha=ref.packet_sha,access=ACCESS)
        receipt=commands(coordinator(h.path)).patch_session(access=ACCESS,recover=True,**saved)
        self.assertTrue(receipt.replayed);self.assertEqual(h.get('legacy')['sessions'][0]['title'],'recovered synthetic title')

    def test_flags_no_automatic_dispatch_or_credential_claim(self):
        for name in ('production_supported','http_integrated','automatic_recovery_supported'):self.assertFalse(getattr(self.store,name))
        self.assertFalse(hasattr(self.store,'dispatch'));self.assertFalse(hasattr(self.store,'recover'))

    def test_real_process_exit_keeps_exact_packet_for_new_process(self):
        code="""import json,os,sys
from dataclasses import asdict
from backend.session_lifecycle_commands import ConfigCommandAccess
from backend.session_lifecycle_request_packets import PrivateCommandPackets
s=PrivateCommandPackets(sys.argv[1],binding='a'*64,authorize=lambda *_:True,enabled=True)
r=s.prepare('request-1','session.patch.v1','api-synthetic',json.loads(sys.stdin.read()),access=ConfigCommandAccess('synthetic_owner'))
print(json.dumps(asdict(r)),flush=True)
os._exit(90)
"""
        p=subprocess.run([sys.executable,'-c',code,str(self.directory)],input=json.dumps(self.body),text=True,capture_output=True,timeout=10)
        self.assertEqual(p.returncode,90,p.stderr);ref=json.loads(p.stdout)
        self.assertEqual(self.store.load('request-1',ACTION,TARGET,expected_sha=ref['packet_sha'],access=ACCESS),self.body)
