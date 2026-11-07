"""Historical status reads against disposable canonical SQLite/private JSON."""
from contextlib import closing
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
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_activation_status import inspect_recorded_activation
from backend.session_window_attempts import WindowAttemptJournal, install_schema
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness, Proxy


def restart_worker(path):
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')):
        with patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')):
            h=FileHarness(path,seed=False)
            try:
                journal=WindowAttemptJournal(h.repo,enabled=True)
                row=journal.inspect(CID,'attempt_1',access=CTX)
                print(json.dumps(inspect_recorded_activation(journal,CID,'activate_1',
                    attempt_request_id='attempt_1',packet_sha256=row.packet_sha256,access=CTX,enabled=True)))
            finally:h.conn.close()


@unittest.skipUnless(os.name=='posix','private JSON fixture uses POSIX')
class StatusTests(unittest.TestCase):
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
        self.journal.record_attempt(CID,'attempt_1',packet_sha256=self.state.packet_sha256,
            expected_revision=self.state.revision,access=CTX)

    def activate(self):
        return activate_recorded_attempt(self.h.engine,self.journal,CID,'activate_1',
            attempt_request_id='attempt_1',packet_sha256=self.state.packet_sha256,access=CTX,enabled=True)

    def status(self,**changes):
        args=dict(attempt_request_id='attempt_1',packet_sha256=self.state.packet_sha256,access=CTX,enabled=True)
        args.update(changes)
        return inspect_recorded_activation(self.journal,CID,'activate_1',**args)

    def confirmed(self):self.record();self.h.confirm();self.activate()

    def rejects(self,fn,category):
        with self.assertRaises(LifecycleError) as caught:fn()
        self.assertEqual(caught.exception.category,category)

    def test_default_off_has_no_io(self):
        self.rejects(lambda:inspect_recorded_activation(None,CID,'id',attempt_request_id='a',
            packet_sha256='',access=None),'lifecycle_disabled')

    def test_missing_attempt_does_not_create_or_authorize_retry(self):
        result=self.status()
        self.assertEqual(result['state'],'attempt_missing');self.assertFalse(result['retry_authorized'])
        self.assertEqual(self.h.conn.execute('SELECT count(*) FROM session_window_attempts').fetchone()[0],0)

    def test_recorded_but_unactivated_is_unconfirmed(self):
        self.record();self.assertEqual(self.status()['state'],'unconfirmed')

    def test_runtime_receipt_alone_does_not_count_as_persisted_activation(self):
        self.record();self.h.confirm()
        with patch.object(self.h,'read_receipt',side_effect=AssertionError('status must not query runtime')):
            self.assertEqual(self.status()['state'],'unconfirmed')
        self.assertEqual(self.h.record(),self.state)

    def test_confirmed_is_historical_not_current_window_or_reply(self):
        self.confirmed()
        result=self.status()
        self.assertEqual(result['state'],'historical_confirmed')
        self.assertTrue(result['historical']);self.assertTrue(result['window_confirmed_at_apply'])
        self.assertFalse(result['current_window_confirmed']);self.assertFalse(result['reply_completed'])
        self.assertFalse(result['retry_authorized'])
        self.assertEqual(result['revision_applied'],self.state.revision+1)

    def test_later_closeout_does_not_hide_original_historical_confirmation(self):
        self.confirmed()
        self.h.engine.closeout(CID,'close_2',window_id='window_1',context_epoch=1,access=CTX)
        with patch.object(self.h,'read_receipt',side_effect=AssertionError('no runtime')):
            self.assertEqual(self.status()['state'],'historical_confirmed')
        self.assertEqual(self.h.record().phase,'closed')

    def test_legacy_activation_receipt_cannot_be_claimed_as_recorded(self):
        self.record();self.h.confirm()
        self.h.engine.activate(CID,'activate_1',packet_sha256=self.state.packet_sha256,access=CTX)
        self.rejects(self.status,'lifecycle_activation_status_conflict')

    def test_wrong_attempt_or_packet_rejected(self):
        self.confirmed()
        self.rejects(lambda:self.status(attempt_request_id='other'),'lifecycle_activation_status_conflict')
        self.rejects(lambda:self.status(packet_sha256='0'*64),'lifecycle_attempt_request_conflict')

    def test_owner_or_scope_change_cannot_read_status(self):
        self.confirmed()
        for access in (AccessScope('other',CTX.allowed_scopes),AccessScope(CTX.principal_id,frozenset())):
            self.rejects(lambda:self.status(access=access),'lifecycle_access_denied')

    def test_corrupt_journal_or_receipt_denied(self):
        self.confirmed()
        self.h.conn.execute('UPDATE session_lifecycle_receipts SET sha256=? WHERE request_id=?',('0'*64,'activate_1'))
        self.rejects(self.status,'lifecycle_storage_corrupt')

    def test_observation_has_query_only_and_deferred_begin_and_cannot_save(self):
        statements=[];original=self.h.repo._connect
        def factory():
            conn=original();conn.set_trace_callback(statements.append);return conn
        self.h.repo._connect=factory
        with self.h.repo.observation(CID) as tx:
            self.assertEqual(tx.conn.execute('PRAGMA query_only').fetchone()[0],1)
            self.rejects(lambda:tx.save(0,None,None),'lifecycle_readonly')
            with self.assertRaises(sqlite3.OperationalError):tx.conn.execute('DELETE FROM messages')
            with closing(self.h.connect_canonical()) as conn:
                conn.execute('BEGIN IMMEDIATE');conn.rollback()
        self.assertIn('BEGIN',statements);self.assertNotIn('BEGIN IMMEDIATE',statements)

    def test_repeated_status_changes_no_canonical_rows_or_config(self):
        self.confirmed()
        before=list(self.h.conn.iterdump());config=self.h.config_path.read_bytes()
        for _ in range(3):self.status()
        self.assertEqual(list(self.h.conn.iterdump()),before);self.assertEqual(self.h.config_path.read_bytes(),config)

    def test_lost_activation_commit_ack_is_readable_without_reactivation(self):
        self.record();self.h.confirm()
        def lost():raise OSError('synthetic lost ACK')
        self.h.connection_wrapper=lambda conn:Proxy(conn,after=lost)
        self.rejects(self.activate,'lifecycle_storage_unavailable')
        self.h.connection_wrapper=None
        with patch.object(self.h.engine,'activate',side_effect=AssertionError('must not activate')):
            self.assertEqual(self.status()['state'],'historical_confirmed')

    def test_new_process_reads_original_status(self):
        self.confirmed()
        result=subprocess.run([sys.executable,'-c',
            'import sys; from backend.tests.test_session_lifecycle_activation_status import restart_worker; restart_worker(sys.argv[1])',
            self.h.path],capture_output=True,text=True,check=True,timeout=20)
        self.assertEqual(json.loads(result.stdout),self.status())

    def test_observed_config_conflict_reports_unavailable_instead_of_success(self):
        self.confirmed();reader=self.h.reader
        class Conflict:
            def capture(_,cid):return reader.capture(cid)
            def is_current(_,view):return False
        self.h.repo._reader=Conflict()
        self.rejects(self.status,'lifecycle_authority_changed')
