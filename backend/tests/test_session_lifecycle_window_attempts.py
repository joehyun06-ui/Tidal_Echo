"""Disposable canonical SQLite/JSON only; no dispatch callback or provider IO."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.g3_projection_contract import AccessScope
from backend.session_lifecycle import LifecycleError
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness, Proxy


def restart_worker(path):
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')):
        h = FileHarness(path, seed=False)
        try:
            journal = WindowAttemptJournal(h.repo, enabled=True)
            old = journal.inspect(CID, 'attempt_1', access=CTX)
            replay = journal.record_attempt(CID, 'attempt_1', packet_sha256=old.packet_sha256,
                                            expected_revision=old.lifecycle_revision, access=CTX)
            print(json.dumps(asdict(replay)))
        finally:
            h.conn.close()


@unittest.skipUnless(os.name == 'posix', 'canonical private JSON fixture uses POSIX')
class AttemptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'canonical.sqlite'
        self.h = FileHarness(self.path)
        self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')))
        with closing(self.h.connect_canonical()) as conn:
            self.assertTrue(install_schema(conn))
        self.journal = WindowAttemptJournal(self.h.repo, enabled=True)
        self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0, access=CTX)
        self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)
        self.state = self.h.record()

    def record(self, request_id='attempt_1', **kwargs):
        return self.journal.record_attempt(CID, request_id,
            **dict(packet_sha256=self.state.packet_sha256, expected_revision=self.state.revision,
                   access=CTX, **kwargs))

    def rejects(self, callback, category):
        with self.assertRaises(LifecycleError) as caught:
            callback()
        self.assertEqual(caught.exception.category, category)

    def count(self):
        return self.h.conn.execute('SELECT count(*) FROM session_window_attempts').fetchone()[0]

    def test_default_off_has_no_repository_io(self):
        journal = WindowAttemptJournal(object())
        self.rejects(lambda: journal.record_attempt(CID, 'id', packet_sha256='0'*64,
                     expected_revision=1, access=CTX), 'lifecycle_disabled')
        self.rejects(lambda: journal.inspect(CID, 'id', access=CTX), 'lifecycle_disabled')

    def test_record_is_unknown_not_permission_and_preserves_all_canonical_inputs(self):
        config = self.h.config_path.read_bytes()
        messages = self.h.conn.execute('SELECT * FROM messages').fetchall()
        receipts = self.h.conn.execute('SELECT * FROM session_lifecycle_receipts').fetchall()
        state = self.h.record()
        result = self.record()
        self.assertFalse(result.replayed)
        self.assertEqual(result.outcome, 'unknown')
        self.assertFalse(result.dispatch_authorized)
        self.assertFalse(result.window_confirmed)
        self.assertFalse(result.reply_completed)
        self.assertFalse(self.journal.runtime_integrated)
        self.assertFalse(self.journal.production_supported)
        self.assertEqual(self.h.config_path.read_bytes(), config)
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(), messages)
        self.assertEqual(self.h.conn.execute('SELECT * FROM session_lifecycle_receipts').fetchall(), receipts)
        self.assertEqual(self.h.record(), state)
        self.assertEqual(self.count(), 1)

    def test_same_request_replays_without_second_row(self):
        first = self.record()
        second = self.record()
        self.assertEqual(second, replace(first, replayed=True))
        self.assertEqual(self.journal.inspect(CID, 'attempt_1', access=CTX), second)
        self.assertEqual(self.count(), 1)

    def test_missing_inspection_does_not_record_attempt(self):
        self.assertIsNone(self.journal.inspect(CID, 'absent', access=CTX))
        self.assertEqual(self.count(), 0)

    def test_different_request_for_same_closeout_is_blocked(self):
        self.record()
        self.rejects(lambda: self.record('attempt_2'), 'lifecycle_attempt_closeout_conflict')
        self.assertEqual(self.count(), 1)

    def test_changed_digest_or_revision_cannot_reuse_request(self):
        self.record()
        for digest, revision in (('0'*64, self.state.revision),
                                 (self.state.packet_sha256, self.state.revision+1)):
            self.rejects(lambda: self.journal.record_attempt(CID, 'attempt_1', packet_sha256=digest,
                expected_revision=revision, access=CTX), 'lifecycle_attempt_request_conflict')
        self.assertEqual(self.count(), 1)

    def test_stale_state_revision_and_packet_rejected_before_insert(self):
        for digest, revision in (('0'*64, self.state.revision),
                                 (self.state.packet_sha256, self.state.revision-1)):
            self.rejects(lambda: self.journal.record_attempt(CID, 'attempt_1', packet_sha256=digest,
                expected_revision=revision, access=CTX), 'lifecycle_attempt_state_changed')
        self.assertEqual(self.count(), 0)

    def test_delta_after_record_does_not_allow_new_request_or_rewrite_history(self):
        first = self.record()
        self.h.engine.reentry_delta(CID, 'delta_1', after_source=self.state.source_cursor,
                                   after_message=self.state.message_cursor, access=CTX)
        self.assertEqual(self.h.record().revision, self.state.revision+1)
        self.assertTrue(self.record().replayed)
        changed = self.h.record()
        self.rejects(lambda: self.journal.record_attempt(CID, 'attempt_2',
            packet_sha256=changed.packet_sha256, expected_revision=changed.revision, access=CTX),
            'lifecycle_attempt_closeout_conflict')
        self.assertEqual(self.journal.inspect(CID, 'attempt_1', access=CTX), replace(first, replayed=True))

    def test_access_scope_change_blocks_record_replay_and_inspection(self):
        self.record()
        for access in (AccessScope('other_owner', CTX.allowed_scopes), AccessScope(CTX.principal_id, frozenset())):
            self.rejects(lambda: self.journal.inspect(CID, 'attempt_1', access=access), 'lifecycle_access_denied')
            self.rejects(lambda: self.journal.record_attempt(CID, 'attempt_1',
                packet_sha256=self.state.packet_sha256, expected_revision=self.state.revision, access=access),
                'lifecycle_access_denied')

    def test_lost_commit_ack_can_only_reconcile_existing_unknown_attempt(self):
        def lost():
            raise OSError('synthetic commit acknowledgement lost')
        self.h.connection_wrapper = lambda conn: Proxy(conn, after=lost)
        self.rejects(self.record, 'lifecycle_storage_unavailable')
        self.h.connection_wrapper = None
        self.assertEqual(self.count(), 1)
        result = self.record()
        self.assertTrue(result.replayed)
        self.assertEqual(result.outcome, 'unknown')
        self.assertFalse(result.dispatch_authorized)
        self.rejects(lambda: self.record('new_id'), 'lifecycle_attempt_closeout_conflict')

    def test_commit_failure_before_commit_rolls_back_attempt(self):
        def fail():
            raise sqlite3.OperationalError('synthetic commit failure')
        self.h.connection_wrapper = lambda conn: Proxy(conn, before=fail)
        self.rejects(self.record, 'lifecycle_storage_unavailable')
        self.h.connection_wrapper = None
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.record().replayed)

    def test_config_conflict_before_commit_rolls_back(self):
        reader = self.h.reader
        class Conflict:
            def capture(_, cid): return reader.capture(cid)
            def is_current(_, view): return False
        self.h.repo._reader = Conflict()
        self.rejects(self.record, 'lifecycle_authority_changed')
        self.assertEqual(self.count(), 0)

    def test_two_independent_repositories_same_closeout_have_one_winner(self):
        def run(request_id):
            h = FileHarness(self.path, seed=False)
            try:
                journal = WindowAttemptJournal(h.repo, enabled=True)
                try:
                    return journal.record_attempt(CID, request_id, packet_sha256=self.state.packet_sha256,
                        expected_revision=self.state.revision, access=CTX).outcome
                except LifecycleError as exc:
                    return exc.category
            finally:
                h.conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ('attempt_a', 'attempt_b')))
        self.assertCountEqual(results, ['unknown', 'lifecycle_attempt_closeout_conflict'])
        self.assertEqual(self.count(), 1)

    def test_restart_reopens_original_request_without_dispatch_permission(self):
        self.record()
        result = subprocess.run([sys.executable, '-c',
            'import sys; from backend.tests.test_session_lifecycle_window_attempts import restart_worker; restart_worker(sys.argv[1])',
            str(self.path)], capture_output=True, text=True, check=True, timeout=20)
        data = json.loads(result.stdout)
        self.assertTrue(data['replayed'])
        self.assertEqual(data['outcome'], 'unknown')
        self.assertFalse(data['dispatch_authorized'])
        self.assertEqual(self.count(), 1)

    def test_corrupt_row_rejected_before_replay_or_inspection(self):
        self.record()
        self.h.conn.execute("UPDATE session_window_attempts SET record_sha256=?", ('0'*64,))
        self.rejects(self.record, 'lifecycle_attempt_corrupt')
        self.rejects(lambda: self.journal.inspect(CID, 'attempt_1', access=CTX), 'lifecycle_attempt_corrupt')

    def test_trigger_rejected_without_executing_it(self):
        self.h.conn.execute('CREATE TRIGGER surprise AFTER INSERT ON session_window_attempts '
                            'BEGIN DELETE FROM messages; END')
        self.rejects(self.record, 'lifecycle_attempt_schema_invalid')
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_install_idempotent_and_refuses_callers_transaction(self):
        with closing(self.h.connect_canonical()) as conn:
            self.assertFalse(install_schema(conn))
            conn.execute('BEGIN IMMEDIATE')
            self.rejects(lambda: install_schema(conn), 'lifecycle_connection_invalid')
            self.assertTrue(conn.in_transaction)
            conn.rollback()

    def test_invalid_parameters_rejected_without_rows(self):
        for digest, revision in (('x'*64, 2), ('0'*64, True), ('0'*64, 0), ('0'*64, 2**63)):
            self.rejects(lambda: self.journal.record_attempt(CID, 'attempt_1',
                packet_sha256=digest, expected_revision=revision, access=CTX), 'lifecycle_attempt_invalid')
        self.assertEqual(self.count(), 0)

    def test_closed_phase_cannot_start_attempt(self):
        # Remove only the disposable prepared state and repeat a real closeout.
        self.h.conn.execute('DELETE FROM session_lifecycle_receipts')
        self.h.conn.execute('DELETE FROM session_lifecycle_records')
        self.h.engine.closeout(CID, 'close_only', window_id='window_0', context_epoch=0, access=CTX)
        self.rejects(self.record, 'lifecycle_phase_conflict')
        self.assertEqual(self.count(), 0)

    def test_codex_remains_explicitly_unsupported(self):
        # The repository still checks config/state binding before this journal.
        config = self.h.get('legacy')
        config['sessions'][0]['provider'] = 'codex'
        self.h.put('legacy', config)
        from backend.session_lifecycle_codec import encode_record, checksum
        payload = encode_record(replace(self.h.record(), provider='codex'))
        self.h.conn.execute('UPDATE session_lifecycle_records SET payload=?,sha256=?', (payload, checksum(payload)))
        self.rejects(self.record, 'lifecycle_provider_unsupported')
        self.assertEqual(self.count(), 0)

    def test_missing_or_wrong_schema_does_not_auto_install(self):
        self.h.conn.execute('DROP TABLE session_window_attempts')
        self.rejects(self.record, 'lifecycle_attempt_schema_invalid')
        self.assertIsNone(self.h.conn.execute("SELECT name FROM sqlite_master WHERE name='session_window_attempts'").fetchone())

    def test_oversized_row_is_not_decoded_into_python(self):
        self.record()
        self.h.conn.execute('UPDATE session_window_attempts SET packet_sha256=?', ('x'*4096,))
        original = self.h.connect_canonical
        def factory():
            conn = original()
            def decode(raw):
                if len(raw) > 4096:
                    raise AssertionError('unbounded data reached decoder')
                return raw.decode('utf-8')
            conn.text_factory = decode
            return conn
        self.h.repo._connect = factory
        self.rejects(self.record, 'lifecycle_attempt_corrupt')

    def test_other_conversation_request_id_does_not_reveal_original(self):
        self.record()
        self.rejects(lambda: self.journal.inspect('absent_conversation', 'attempt_1', access=CTX),
                     'lifecycle_session_unavailable')


if __name__ == '__main__':
    unittest.main()
