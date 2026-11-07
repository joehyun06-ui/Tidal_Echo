"""Synthetic packet handoff; no real provider or model receives source text."""
from contextlib import closing
from dataclasses import FrozenInstanceError, asdict
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError, _json
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.session_window_attempt_dispatch import attempt_packet_dispatch_once
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness, Proxy


@unittest.skipUnless(os.name == 'posix', 'private JSON fixture uses POSIX')
class PacketDispatchTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.h=FileHarness(Path(tmp.name)/'canonical.sqlite');self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')))
        with closing(self.h.connect_canonical()) as conn:install_schema(conn)
        self.journal=WindowAttemptJournal(self.h.repo,enabled=True)
        self.h.engine.closeout(CID,'close_1',window_id='window_0',context_epoch=0,access=CTX)
        self.h.engine.reentry(CID,'prepare_1',closeout_id='close_1',target_window_id='window_1',access=CTX)
        self.state=self.h.record();self.calls=[]

    def run_attempt(self,dispatch=None,**changes):
        if dispatch is None:
            def dispatch(observation,packet):self.calls.append((observation,packet))
        args=dict(packet_sha256=self.state.packet_sha256,expected_revision=self.state.revision,
                  access=CTX,dispatch=dispatch,enabled=True)
        args.update(changes)
        return attempt_packet_dispatch_once(self.h.engine,self.journal,CID,'attempt_1',**args)

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category)

    def test_default_off_does_not_release_packet(self):
        self.rejects(lambda:attempt_packet_dispatch_once(None,None,CID,'id',packet_sha256='',
            expected_revision=0,access=None,dispatch=None),'lifecycle_disabled')

    def test_exact_derived_packet_digest_identity_cursors_and_budget(self):
        result=self.run_attempt();observation,packet=self.calls[0]
        self.assertEqual(hashlib.sha256(_json(asdict(packet))).hexdigest(),observation.packet_sha256)
        self.assertEqual((packet.conversation_id,packet.version_id,packet.provider),(CID,CID,'api'))
        self.assertEqual((packet.from_window_id,packet.target_window_id,packet.context_epoch),('window_0','window_1',1))
        self.assertEqual(packet.source_cursor,self.state.source_cursor)
        self.assertEqual(packet.message_cursor,self.state.message_cursor)
        self.assertEqual(packet.canonical_message_ids,self.state.message_refs)
        self.assertEqual(packet.pending,self.state.pending)
        self.assertEqual(packet.authority,'derived')
        self.assertTrue(all(card.authority=='derived' for card in packet.anchors+packet.recent))
        self.assertLessEqual(len(_json(asdict(packet))),self.state.packet_budget_bytes)
        self.assertEqual(result.outcome,'unknown');self.assertFalse(result.window_confirmed)
        with self.assertRaises(FrozenInstanceError):packet.target_window_id='different'
        with self.assertRaises(FrozenInstanceError):packet.anchors[0].projection_text='different'

    def test_last_validated_packet_is_delivered_not_rebuilt_after_commit(self):
        built=[];original=self.h.engine._packet
        def build(*args,**kwargs):
            result=original(*args,**kwargs);built.append(result[0]);return result
        with patch.object(self.h.engine,'_packet',side_effect=build):self.run_attempt()
        self.assertEqual(len(built),2)
        self.assertIs(self.calls[0][1],built[-1])

    def test_replay_does_not_release_body_again(self):
        self.run_attempt();self.assertTrue(self.run_attempt().replayed)
        self.assertEqual(len(self.calls),1)

    def test_revised_or_revoked_source_never_releases_stale_body(self):
        self.h.g3._denied.add((CTX.principal_id,'private_A'))
        self.rejects(self.run_attempt,'lifecycle_delta_required')
        self.assertEqual(self.calls,[])

    def test_changed_packet_digest_rejected_without_body(self):
        self.rejects(lambda:self.run_attempt(packet_sha256='0'*64),'lifecycle_packet_changed')
        self.assertEqual(self.calls,[])
        self.assertIsNone(self.journal.inspect(CID,'attempt_1',access=CTX))

    def test_lost_commit_ack_replay_never_releases_body(self):
        def lost():raise OSError('synthetic commit ACK lost')
        self.h.connection_wrapper=lambda conn:Proxy(conn,after=lost)
        self.rejects(self.run_attempt,'lifecycle_storage_unavailable')
        self.h.connection_wrapper=None
        self.assertTrue(self.run_attempt().replayed);self.assertEqual(self.calls,[])

    def test_postcommit_authority_change_has_no_body_delivery(self):
        checks=[]
        def validate(snapshot,*,access):checks.append(1);return len(checks)<3
        with patch.object(self.h,'validate',side_effect=validate):
            self.rejects(self.run_attempt,'lifecycle_authority_changed_after_commit')
        self.assertTrue(self.run_attempt().replayed);self.assertEqual(self.calls,[])

    def test_adapter_failure_does_not_release_again(self):
        def dispatch(observation,packet):
            self.calls.append(packet);raise RuntimeError('synthetic adapter error')
        self.rejects(lambda:self.run_attempt(dispatch),'lifecycle_dispatch_unknown')
        self.assertTrue(self.run_attempt(dispatch).replayed)
        self.assertEqual(len(self.calls),1)

    def test_fake_confirmation_return_is_ignored_and_canonical_history_unchanged(self):
        config=self.h.config_path.read_bytes();messages=self.h.conn.execute('SELECT * FROM messages').fetchall()
        def dispatch(observation,packet):return {'window_confirmed':True,'status':'confirmed'}
        result=self.run_attempt(dispatch)
        self.assertFalse(result.window_confirmed);self.assertFalse(result.dispatch_authorized)
        self.assertEqual(self.h.record(),self.state)
        self.assertEqual(self.h.config_path.read_bytes(),config)
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(),messages)
