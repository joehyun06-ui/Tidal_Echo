"""Synthetic runtime receipts only; real disposable canonical SQLite/JSON."""
from contextlib import closing
from dataclasses import replace
import os
import json
import subprocess
import sys
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_attempt_dispatch import attempt_packet_dispatch_once
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness, Proxy


def restart_worker(path):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')):
        with patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')):
            h=FileHarness(path,seed=False)
            try:
                result=activate_recorded_attempt(h.engine,WindowAttemptJournal(h.repo,enabled=True),
                    CID,'activate_1',attempt_request_id='attempt_1',
                    packet_sha256=h.record().packet_sha256,access=CTX,enabled=True)
                print(json.dumps({'replayed':result.replayed,'confirmed':result.window_confirmed,
                                  'revision':h.record().revision}))
            finally:h.conn.close()


@unittest.skipUnless(os.name == 'posix', 'private JSON fixture uses POSIX')
class RecordedActivationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.h=FileHarness(Path(tmp.name)/'canonical.sqlite');self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')))
        with closing(self.h.connect_canonical()) as conn:install_schema(conn)
        self.journal=WindowAttemptJournal(self.h.repo,enabled=True)
        self.h.engine.closeout(CID,'close_1',window_id='window_0',context_epoch=0,access=CTX)
        self.h.engine.reentry(CID,'prepare_1',closeout_id='close_1',target_window_id='window_1',access=CTX)
        self.state=self.h.record()

    def record(self):
        return self.journal.record_attempt(CID,'attempt_1',packet_sha256=self.state.packet_sha256,
            expected_revision=self.state.revision,access=CTX)

    def activate(self,**changes):
        args=dict(attempt_request_id='attempt_1',packet_sha256=self.state.packet_sha256,access=CTX,enabled=True)
        args.update(changes)
        return activate_recorded_attempt(self.h.engine,self.journal,CID,'activate_1',**args)

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category)

    def test_default_off_touches_no_dependencies(self):
        self.rejects(lambda:activate_recorded_attempt(None,None,CID,'id',attempt_request_id='a',
            packet_sha256='',access=None),'lifecycle_disabled')

    def test_missing_attempt_refuses_even_with_confirmed_runtime_receipt(self):
        self.h.confirm()
        self.rejects(self.activate,'lifecycle_attempt_missing')
        self.assertEqual(self.h.record(),self.state)

    def test_record_alone_cannot_activate_without_runtime_receipt(self):
        self.record()
        self.rejects(self.activate,'lifecycle_confirmation_unknown')
        self.assertEqual(self.h.record(),self.state)

    def test_unknown_runtime_receipt_cannot_activate(self):
        self.record();self.h.confirm(status='unknown')
        self.rejects(self.activate,'lifecycle_confirmation_unknown')

    def test_mismatched_receipt_is_not_promoted(self):
        self.record();self.h.confirm(target_window_id='other_window')
        self.rejects(self.activate,'lifecycle_confirmation_mismatch')
        self.assertEqual(self.h.record(),self.state)

    def test_confirmed_attempt_activates_once_and_keeps_journal_unknown(self):
        self.record();self.h.confirm()
        messages=self.h.conn.execute('SELECT * FROM messages').fetchall();config=self.h.config_path.read_bytes()
        result=self.activate()
        self.assertTrue(result.window_confirmed);self.assertFalse(result.reply_completed)
        self.assertEqual((self.h.record().conversation_id,self.h.record().window_id),(CID,'window_1'))
        self.assertEqual(self.h.record().revision,self.state.revision+1)
        self.assertTrue(self.activate().replayed)
        observation=self.journal.inspect(CID,'attempt_1',access=CTX)
        self.assertEqual(observation.outcome,'unknown');self.assertFalse(observation.window_confirmed)
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(),messages)
        self.assertEqual(self.h.config_path.read_bytes(),config)

    def test_changed_packet_or_delta_revision_refuses_original_attempt(self):
        self.record()
        self.h.engine.reentry_delta(CID,'delta_1',after_source=self.state.source_cursor,
            after_message=self.state.message_cursor,access=CTX)
        self.rejects(self.activate,'lifecycle_attempt_state_changed')

    def test_activation_request_is_bound_to_attempt_id(self):
        self.record();self.h.confirm();self.activate()
        self.rejects(lambda:self.activate(attempt_request_id='other_attempt'),'lifecycle_attempt_missing')
        # Legacy activation cannot reuse the recorded activation's receipt ID.
        self.rejects(lambda:self.h.engine.activate(CID,'activate_1',packet_sha256=self.state.packet_sha256,
            access=CTX),'lifecycle_request_conflict')

    def test_corrupt_attempt_blocks_before_runtime_read(self):
        self.record();self.h.confirm()
        self.h.conn.execute('UPDATE session_window_attempts SET record_sha256=?',('0'*64,))
        with patch.object(self.h,'read_receipt',side_effect=AssertionError('must not query runtime')):
            self.rejects(self.activate,'lifecycle_attempt_corrupt')

    def test_lost_activation_commit_ack_reconciles_original_receipt(self):
        self.record();self.h.confirm()
        def lost():raise OSError('synthetic activation commit ACK lost')
        self.h.connection_wrapper=lambda conn:Proxy(conn,after=lost)
        self.rejects(self.activate,'lifecycle_storage_unavailable')
        self.h.connection_wrapper=None
        self.assertEqual(self.h.record().phase,'active')
        self.assertTrue(self.activate().replayed)
        self.assertEqual(self.h.record().revision,self.state.revision+1)

    def test_commit_failure_rolls_back_state_and_activation_receipt(self):
        self.record();self.h.confirm()
        def fail():raise OSError('synthetic before commit failure')
        self.h.connection_wrapper=lambda conn:Proxy(conn,before=fail)
        self.rejects(self.activate,'lifecycle_storage_unavailable')
        self.h.connection_wrapper=None
        self.assertEqual(self.h.record(),self.state)
        self.assertIsNone(self.h.conn.execute("SELECT 1 FROM session_lifecycle_receipts WHERE request_id='activate_1'").fetchone())

    def test_packet_handoff_and_independent_synthetic_receipt_complete_same_chat(self):
        delivered=[]
        def dispatch(observation,packet):
            delivered.append((observation.request_id,packet.target_window_id))
            # A synthetic independent runtime observation, not a callback return.
            self.h.confirm()
            return {'window_confirmed':False}
        sent=attempt_packet_dispatch_once(self.h.engine,self.journal,CID,'attempt_1',
            packet_sha256=self.state.packet_sha256,expected_revision=self.state.revision,
            access=CTX,dispatch=dispatch,enabled=True)
        self.assertFalse(sent.window_confirmed)
        self.assertTrue(self.activate().window_confirmed)
        self.assertEqual(delivered,[('attempt_1','window_1')])
        self.assertEqual(self.h.record().conversation_id,CID)

    def test_new_process_reconciles_same_durable_activation(self):
        self.record();self.h.confirm();self.activate()
        result=subprocess.run([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_recorded_activation import restart_worker; restart_worker(sys.argv[1])',
            self.h.path],capture_output=True,text=True,check=True,timeout=20)
        value=json.loads(result.stdout)
        self.assertTrue(value['replayed']);self.assertTrue(value['confirmed'])
        self.assertEqual(value['revision'],self.state.revision+1)
