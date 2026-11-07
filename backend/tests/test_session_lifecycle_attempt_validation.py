"""Real disposable repository plus synthetic fenced G3/runtime authority."""
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from backend.session_lifecycle import LifecycleError
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.session_window_attempt_validation import record_validated_attempt
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness


@unittest.skipUnless(os.name == 'posix', 'private JSON repository fixture uses POSIX')
class ValidationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.h = FileHarness(Path(tmp.name)/'canonical.sqlite')
        self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')))
        with closing(self.h.connect_canonical()) as conn: install_schema(conn)
        self.journal = WindowAttemptJournal(self.h.repo, enabled=True)
        self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0, access=CTX)
        self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)
        self.state = self.h.record()

    def record(self, **changes):
        args = dict(packet_sha256=self.state.packet_sha256, expected_revision=self.state.revision,
                    access=CTX, enabled=True)
        args.update(changes)
        return record_validated_attempt(self.h.engine, self.journal, CID, 'attempt_1', **args)

    def count(self):
        return self.h.conn.execute('SELECT count(*) FROM session_window_attempts').fetchone()[0]

    def rejects(self, fn, category):
        with self.assertRaises(LifecycleError) as caught: fn()
        self.assertEqual(caught.exception.category, category)

    def test_default_off_touches_no_dependencies(self):
        self.rejects(lambda: record_validated_attempt(None, None, CID, 'id', packet_sha256='',
                     expected_revision=0, access=None), 'lifecycle_disabled')

    def test_validated_record_and_replay_remain_unknown_without_runtime_receipt(self):
        before = self.h.record()
        result = self.record()
        self.assertEqual(result.outcome, 'unknown')
        self.assertFalse(result.dispatch_authorized)
        self.assertFalse(result.window_confirmed)
        self.assertEqual(self.record(), replace(result, replayed=True))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.h.record(), before)

    def test_mismatched_repository_rejected_before_fence(self):
        other = WindowAttemptJournal(object(), enabled=True)
        self.rejects(lambda: record_validated_attempt(self.h.engine, other, CID, 'attempt_1',
            packet_sha256=self.state.packet_sha256, expected_revision=self.state.revision,
            access=CTX, enabled=True), 'lifecycle_attempt_binding_invalid')
        self.assertEqual(self.count(), 0)

    def test_new_source_requires_delta_before_recording(self):
        self.h.source('event_2', 'synthetic new evidence')
        self.rejects(self.record, 'lifecycle_delta_required')
        self.assertEqual(self.count(), 0)

    def test_new_message_requires_delta_before_recording(self):
        self.h.conn.execute("INSERT INTO messages(ts,kind,direction,text,meta) VALUES "
                            "('2026-11-07T00:00:00Z','user','in','synthetic new message',?)",
                            ('{"api_session":"'+CID+'"}',))
        self.rejects(self.record, 'lifecycle_delta_required')
        self.assertEqual(self.count(), 0)

    def test_acl_revocation_blocks_current_packet_and_historical_validated_replay(self):
        self.record()
        self.h.g3._denied.add((CTX.principal_id, 'private_A'))
        self.rejects(self.record, 'lifecycle_delta_required')
        # Reconciliation remains a historical read, never a new validation grant.
        old = self.journal.inspect(CID, 'attempt_1', access=CTX)
        self.assertEqual(old.outcome, 'unknown')
        self.assertFalse(old.dispatch_authorized)
        self.assertEqual(self.count(), 1)

    def test_revised_evidence_requires_delta(self):
        self.h.source('event_1', 'synthetic revised', revision=2)
        self.rejects(self.record, 'lifecycle_delta_required')
        self.assertEqual(self.count(), 0)

    def test_unsafe_stream_tool_or_unknown_dispatch_refuses_before_insert(self):
        original = self.h.get('runtime')
        for field in ('streaming', 'tool_inflight', 'uncertain_dispatch'):
            runtime = dict(original); runtime[field] = True; self.h.put('runtime', runtime)
            self.rejects(self.record, 'lifecycle_unsafe_boundary')
            self.assertEqual(self.count(), 0)

    def test_authority_changes_during_precommit_validation_roll_back(self):
        self.h.validation_hook = lambda: self.h.g3.add_revision('event_2', 'synthetic changed', cursor=3)
        self.rejects(self.record, 'lifecycle_authority_changed')
        self.assertEqual(self.count(), 0)

    def test_false_postinsert_validation_rolls_back_insert(self):
        calls = []
        def validate(snapshot, *, access):
            calls.append(1)
            return len(calls) == 1
        with patch.object(self.h, 'validate', side_effect=validate):
            self.rejects(self.record, 'lifecycle_authority_changed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.count(), 0)

    def test_postcommit_failure_retains_unknown_original_row(self):
        calls = []
        def validate(snapshot, *, access):
            calls.append(1)
            return len(calls) < 3
        with patch.object(self.h, 'validate', side_effect=validate):
            self.rejects(self.record, 'lifecycle_authority_changed_after_commit')
        self.assertEqual(self.count(), 1)
        self.assertTrue(self.journal.inspect(CID, 'attempt_1', access=CTX).replayed)
        self.assertTrue(self.record().replayed)
        self.assertEqual(self.count(), 1)

    def test_wrong_digest_or_revision_has_no_record(self):
        self.rejects(lambda: self.record(packet_sha256='0'*64), 'lifecycle_packet_changed')
        self.rejects(lambda: self.record(expected_revision=self.state.revision+1), 'lifecycle_attempt_state_changed')
        self.assertEqual(self.count(), 0)
