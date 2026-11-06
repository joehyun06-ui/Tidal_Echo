"""Fixed API version packets, exact recovery and independent authorization."""
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
from backend.session_lifecycle_version_packet_binding import PrivateApiVersionCreate,CONTRACT
from backend.session_lifecycle_api_versions import CREATE
from backend.tests import test_session_lifecycle_api_versions as fixtures
from backend.tests.test_session_lifecycle_api_versions import CID,A,TARGET,BODY,TITLE,STAMP,ACCESS,writer
from backend.tests.test_session_lifecycle_coordination import coordinator


def adapter(w,directory,*,authorize=lambda *_:True,enabled=True):
    c=w.coordinator;binding=digest(encode([str(w.canonical_path),str(c.config),str(c.directory)]))
    p=PrivateCommandPackets(directory,binding=binding,authorize=authorize,enabled=True)
    return PrivateApiVersionCreate(w,p,session_lock=w.session_lock,enabled=enabled)


@unittest.skipUnless(os.name=='posix','private POSIX version adapter')
class VersionPacketBindingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ApiVersionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.directory=self.f.root/'private-packets';self.directory.mkdir(mode=0o700)
        self.a=adapter(self.f.w,self.directory)
    def prepare(self,*,source=CID,body=None,select=True):
        return self.a.prepare(CID,source,BODY if body is None else body,title=TITLE,created_at=STAMP,select=select,access=ACCESS)
    def execute(self,ref,**kw):return self.a.execute(CID,ref,access=kw.pop('access',ACCESS),**kw)
    def reopen(self,authorize=lambda *_:True):
        return adapter(writer(coordinator(self.f.h.path),self.f.h.path,authorize=authorize),self.directory)
    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category);self.assertEqual(str(caught.exception),category)

    def test_disabled_and_global_denied_precede_io(self):
        a=PrivateApiVersionCreate(None,None,session_lock=None)
        self.rejects(lambda:a.prepare(CID,CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS),'lifecycle_disabled')
        a=adapter(writer(self.f.c,self.f.h.path,authorize=lambda *_:False),Path('/missing'))
        self.rejects(lambda:a.prepare(CID,CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS),'coordination_access_denied')

    def test_prepare_is_private_and_query_only_for_canonical_stores(self):
        raw=self.f.h.config_path.read_bytes();rows=self.f.h.conn.execute('SELECT * FROM messages').fetchall()
        ref=self.prepare();self.assertEqual(ref.request_id,TARGET);self.assertEqual(self.prepare(),ref)
        self.assertEqual(self.f.h.config_path.read_bytes(),raw);self.assertEqual(self.f.h.conn.execute('SELECT * FROM messages').fetchall(),rows)
        self.assertEqual(self.f.count('session_coordination_ops'),0);self.assertIsNone(self.f.edge())
        packet=self.a.packets.load(TARGET,CREATE,CID,expected_sha=ref.packet_sha,access=ACCESS)
        self.assertEqual(packet['preconditions'],self.f.request);self.assertEqual(packet['body'],BODY)
        self.assertEqual((packet['title'],packet['created_at'],packet['select']),(TITLE,STAMP,True))
        for private in ('synthetic private text','synthetic secret','synthetic shared tone'):self.assertNotIn(private,str(packet))

    def test_original_group_source_and_target_write_scopes_required(self):
        for denied in (CID,A,TARGET):
            a=adapter(writer(self.f.c,self.f.h.path,authorize=lambda _a,action,sid:action!=CREATE or sid!=denied),self.directory)
            self.rejects(lambda:a.prepare(CID,CID,BODY,title=TITLE,created_at=STAMP,access=ACCESS),'coordination_access_denied')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_matching_lock_and_resource_binding_precede_packet_io(self):
        self.a.session_lock=RLock();self.rejects(self.prepare,'coordination_lock_mismatch')
        self.a.session_lock=self.f.w.session_lock;self.a.packets.binding='0'*64
        self.rejects(self.prepare,'coordination_resource_mismatch');self.assertEqual(list(self.directory.iterdir()),[])

    def test_storage_permission_does_not_replace_concrete_command_authorization(self):
        ref=self.prepare();a=self.reopen(authorize=lambda _a,_b,sid:sid!=A)
        self.rejects(lambda:a.execute(CID,ref,access=ACCESS),'coordination_access_denied');self.assertIsNone(self.f.edge())

    def test_storage_denial_and_file_damage_block_execution(self):
        ref=self.prepare();self.a.packets.authorize=lambda *_:False
        self.rejects(lambda:self.execute(ref),'coordination_access_denied');self.a.packets.authorize=lambda *_:True
        next(self.directory.glob('*.json')).chmod(0o644)
        self.rejects(lambda:self.execute(ref),'coordination_packet_private_file_required');self.assertIsNone(self.f.edge())

    def test_fixed_packet_executes_once_without_new_preflight(self):
        ref=self.prepare()
        with patch.object(self.a.commands,'snapshot_version',side_effect=AssertionError('must not re-preflight')):
            self.assertFalse(self.execute(ref).replayed);self.assertTrue(self.execute(ref).replayed)
        self.assertEqual(len(self.f.rows()),2);self.assertEqual(self.f.selected(),TARGET)
        self.assertEqual(len([r for r in self.f.config()['sessions'] if r['id']==TARGET]),1)

    def test_source_change_requires_new_explicit_preflight_not_silent_rebase(self):
        ref=self.prepare();self.f.h.conn.execute("UPDATE messages SET text='later source text' WHERE id=2")
        self.rejects(lambda:self.execute(ref),'coordination_version_source_changed');self.assertIsNone(self.f.edge())

    def test_actor_root_digest_and_reference_types_are_bound(self):
        ref=self.prepare()
        self.rejects(lambda:self.execute(ref,access=ConfigCommandAccess('other')),'coordination_packet_conflict')
        self.rejects(lambda:self.a.execute(A,ref,access=ACCESS),'coordination_packet_conflict')
        self.rejects(lambda:self.execute(PacketReference(TARGET,'0'*64)),'coordination_packet_changed')
        self.rejects(lambda:self.execute({'request_id':TARGET}),'coordination_version_packet_invalid')
        self.rejects(lambda:self.execute(ref,recover=1),'coordination_version_packet_invalid')

    def test_select_false_is_persisted_and_preserves_prior_selection(self):
        self.f.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,A));ref=self.prepare(select=False)
        self.execute(ref);self.assertEqual(self.f.selected(),A);self.assertEqual(len(self.f.rows()),2)

    def test_nested_edit_first_prompt_keeps_empty_prefix_and_parent(self):
        body={**BODY,'mode':'edit','message_id':self.f.rows(A)[0]['id']}
        ref=self.prepare(source=A,body=body);self.execute(ref)
        self.assertEqual(self.f.rows(),[]);self.assertEqual(self.f.edge()['parent_id'],A)

    def test_same_target_changed_point_or_select_cannot_replace_original_packet(self):
        self.prepare();self.rejects(lambda:self.prepare(select=False),'coordination_packet_conflict')
        self.rejects(lambda:self.prepare(body={**BODY,'message_id':6}),'coordination_packet_conflict')

    def test_uuid_text_alias_has_one_canonical_operation_identity(self):
        ref=self.prepare();alias={**BODY,'request_id':BODY['request_id'].upper()}
        self.assertEqual(self.prepare(body=alias),ref);self.execute(ref)
        self.assertEqual(len(self.f.rows()),2);self.assertEqual(self.f.count('session_coordination_ops'),1)

    def test_packet_cannot_supply_access_dispatch_or_changed_target_uuid(self):
        ref=self.prepare();packet=self.a.packets.load(TARGET,CREATE,CID,expected_sha=ref.packet_sha,access=ACCESS)
        for extra in ({'access':{'principal_id':'admin'}},{'recover':True},{'method':'other'}):
            with patch.object(self.a.packets,'load',return_value={**packet,**extra}):
                self.rejects(lambda:self.execute(ref),'coordination_version_packet_invalid')
        changed={**packet,'body':{**BODY,'request_id':'52345678-1234-4234-8234-123456789abc'}}
        with patch.object(self.a.packets,'load',return_value=changed):
            self.rejects(lambda:self.execute(ref),'coordination_version_packet_invalid')

    def test_prepared_only_recovery_cannot_start_copy(self):
        ref=self.prepare();self.rejects(lambda:self.execute(ref,recover=True),'coordination_request_missing')
        self.assertIsNone(self.f.edge());self.assertEqual(self.f.count('session_coordination_ops'),0)

    def test_actual_process_exit_reopens_exact_packet_without_recopied_history(self):
        ref=self.prepare();code="""import json,os,sys
from pathlib import Path
from unittest.mock import patch
from backend.tests.test_session_lifecycle_version_packet_binding import adapter
from backend.tests.test_session_lifecycle_api_versions import writer,CID,ACCESS
from backend.tests.test_session_lifecycle_coordination import coordinator
from backend.session_lifecycle_request_packets import PacketReference
a=adapter(writer(coordinator(sys.argv[1]),sys.argv[1]),Path(sys.argv[2]))
with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=lambda *_:os._exit(121)):
    a.execute(CID,PacketReference(**json.loads(sys.stdin.read())),access=ACCESS)
"""
        p=subprocess.run([sys.executable,'-c',code,self.f.h.path,str(self.directory)],input=json.dumps({'request_id':ref.request_id,'packet_sha':ref.packet_sha}),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,121,p.stderr);copied=self.f.rows();self.assertEqual(len(copied),2)
        self.f.h.conn.execute("UPDATE messages SET text='changed source after intent' WHERE id=2")
        reopened=self.reopen()
        with patch.object(reopened.commands,'snapshot_version',side_effect=AssertionError('no new source snapshot')):
            self.assertTrue(reopened.execute(CID,ref,access=ACCESS,recover=True).replayed)
        self.assertEqual(self.f.rows(),copied);self.assertTrue(self.execute(ref).replayed)

    def test_pending_new_target_input_refuses_publication_without_repurging(self):
        ref=self.prepare();old=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','new target input',?)",(STAMP,json.dumps({'api_session':TARGET})))
        self.rejects(lambda:self.reopen().execute(CID,ref,access=ACCESS,recover=True),'coordination_version_effect_changed')
        self.assertEqual(self.f.h.config_path.read_bytes(),old);self.assertEqual(len(self.f.rows()),3)

    def test_pending_group_revocation_still_blocks_recovery(self):
        ref=self.prepare();old=self.f.h.config_path.read_bytes()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):self.execute(ref)
        a=self.reopen(authorize=lambda _a,_b,sid:sid!=A)
        self.rejects(lambda:a.execute(CID,ref,access=ACCESS,recover=True),'coordination_access_denied')
        self.assertEqual(self.f.h.config_path.read_bytes(),old)

    def test_after_publication_recovery_preserves_later_input_and_selection(self):
        ref=self.prepare();from backend.deployment_config import atomic_write_text
        def published(*args):atomic_write_text(*args);raise SystemExit('synthetic after publication')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=published):
            with self.assertRaises(SystemExit):self.execute(ref)
        self.f.h.conn.execute('UPDATE web_version_selections SET version_id=? WHERE root_id=?',(A,CID))
        self.f.h.conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,'in','user','later input',?)",(STAMP,json.dumps({'api_session':TARGET})))
        before=self.f.rows();self.assertTrue(self.reopen().execute(CID,ref,access=ACCESS,recover=True).replayed)
        self.assertEqual(self.f.selected(),A);self.assertEqual(self.f.rows(),before)

    def test_flags_remain_off(self):
        for flag in ('production_supported','http_integrated','automatic_recovery_supported'):self.assertFalse(getattr(self.a,flag))
