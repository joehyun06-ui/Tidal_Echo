"""Real store DDL, synthetic rows, read-only observation; no workers or models."""
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import api_web_generation, codex_generation_store as codex
from backend.session_lifecycle import LifecycleError
from backend.session_lifecycle_observation import RuntimeStoreObserver, readonly_factory
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness


class ObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.h = FileHarness(self.root/'relay.sqlite'); self.addCleanup(self.h.conn.close)
        self.api = api_web_generation.Store(self.h.path); self.api.initialize()
        self.native = self.root/'codex.sqlite'
        self.observer = RuntimeStoreObserver(readonly_factory(self.h.path), self.h.reader,
            connect_codex=readonly_factory(self.native), enabled=True)
        self.enterContext(patch.object(socket.socket,'connect', side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline')))

    def rejects(self, function, category):
        with self.assertRaises(LifecycleError) as caught: function()
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(str(caught.exception), category)

    def observe(self): return self.observer.observe(CID)

    def use_codex(self):
        config = self.h.get('legacy'); config['sessions'][0]['provider'] = 'codex'; self.h.put('legacy', config)
        codex.initialize(self.native)
        codex.pin_session(self.native, api_session=CID, model='synthetic', model_provider='synthetic',
                          reasoning_effort=None, persona_hash='a'*64)

    def codex_job(self):
        return codex.enqueue_job(self.native, api_session=CID, canonical_message_id=1, input_digest='a'*64,
            generation_id='synthetic_generation', client_message_id='synthetic_client', callback_identity='synthetic_callback')

    def assert_incomplete(self, result):
        self.assertFalse(result.handoff_supported); self.assertFalse(result.snapshot_complete)
        self.assertFalse(result.stores_atomic)
        for name in ('runtime_idle','window_id','context_epoch','consumed_source_cursor','consumed_message_cursor',
                     'stream_active','tool_inflight','confirmed_receipt'):
            self.assertIsNone(getattr(result, name))

    def test_default_off_never_opens_or_captures(self):
        def forbidden(): self.fail('IO called')
        self.rejects(lambda: RuntimeStoreObserver(forbidden, None).observe(CID), 'lifecycle_disabled')

    def test_api_quiet_database_never_claims_runtime_or_handoff_ready(self):
        result = self.observe()
        self.assertEqual(result.job_counts, ()); self.assertEqual(result.active_jobs, 0)
        self.assertTrue(result.session_available); self.assertIsNone(result.compaction_pending)
        self.assert_incomplete(result)

    def test_api_actual_accept_observed_without_dispatch(self):
        self.api.accept(CID, 'synthetic input', {'api_session':CID})
        before = self.h.conn.total_changes
        result = self.observe()
        self.assertEqual(result.job_counts, (('queued',1),)); self.assertEqual(result.active_jobs, 1)
        self.assertEqual(self.h.conn.total_changes, before); self.assert_incomplete(result)

    def test_api_terminal_unknown_effect_is_retained(self):
        row = self.api.accept(CID, 'synthetic', {'api_session':CID})
        self.h.conn.execute("UPDATE api_web_generations SET status='failed',upstream_unknown=1 WHERE message_id=?", (row['id'],))
        result = self.observe()
        self.assertEqual(result.active_jobs, 0); self.assertEqual(result.explicit_uncertain_jobs, 1)
        self.assert_incomplete(result)

    def test_api_running_and_stopping_are_active(self):
        row = self.api.accept(CID, 'synthetic', {'api_session':CID})
        for status in ('running','stopping'):
            with self.subTest(status=status):
                self.h.conn.execute('UPDATE api_web_generations SET status=? WHERE message_id=?', (status,row['id']))
                self.assertEqual(self.observe().active_jobs, 1)

    def test_api_closed_session_unavailable(self):
        self.h.conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)', (CID,))
        self.assertFalse(self.observe().session_available)

    def test_unknown_job_status_fails_closed(self):
        row = self.api.accept(CID, 'synthetic', {'api_session':CID})
        self.h.conn.execute("UPDATE api_web_generations SET status='future_unreviewed' WHERE message_id=?", (row['id'],))
        self.rejects(self.observe, 'observation_schema_unavailable')

    def test_missing_schema_not_created_by_observation(self):
        self.h.conn.execute('DROP TABLE api_web_generations')
        before = self.h.conn.execute('SELECT name FROM sqlite_master').fetchall()
        self.rejects(self.observe, 'observation_schema_unavailable')
        self.assertEqual(before, self.h.conn.execute('SELECT name FROM sqlite_master').fetchall())

    def test_missing_native_file_is_not_created(self):
        config = self.h.get('legacy'); config['sessions'][0]['provider'] = 'codex'; self.h.put('legacy', config)
        self.rejects(self.observe, 'observation_store_unavailable')
        self.assertFalse(self.native.exists())

    def test_codex_unconfigured_factory_fails_explicitly(self):
        self.use_codex(); self.observer._codex = None
        self.rejects(self.observe, 'observation_codex_store_required')

    def test_codex_queued_and_uncertain_jobs_are_observed_not_recovered(self):
        self.use_codex(); self.codex_job()
        self.assertEqual(self.observe().active_jobs, 1)
        with closing(codex.connect(self.native)) as conn:
            conn.execute("UPDATE codex_generation_jobs SET status='dispatch_uncertain'")
        result = self.observe()
        self.assertEqual(result.job_counts, (('dispatch_uncertain',1),))
        self.assertEqual(result.explicit_uncertain_jobs, 1)
        self.assertIsNone(result.compaction_pending); self.assert_incomplete(result)
        self.assertEqual(codex.get_job(self.native,1)['status'], 'dispatch_uncertain')

    def test_compaction_missing_is_unknown_and_nonterminal_is_pending(self):
        self.use_codex()
        self.assertIsNone(self.observe().compaction_pending)
        with closing(codex.connect(self.native)) as conn:
            # Exact deployed compaction table DDL; no runtime is initialized.
            conn.execute('''CREATE TABLE web_codex_compactions (
                request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                status TEXT NOT NULL, turn_id TEXT, item_done INTEGER NOT NULL DEFAULT 0,
                before_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            self.assertEqual(self.observe().compaction_pending, 0)
            conn.execute("INSERT INTO web_codex_compactions(request_id,session_id,thread_id,status,created_at,updated_at) VALUES ('synthetic',?,'synthetic_thread','unknown','synthetic','synthetic')", (CID,))
            self.assertEqual(self.observe().compaction_pending, 1)
            conn.execute("UPDATE web_codex_compactions SET status='reconcile_queued'")
            self.assertEqual(self.observe().compaction_pending, 1)
            conn.execute("UPDATE web_codex_compactions SET status='completed'")
            self.assertEqual(self.observe().compaction_pending, 0)
        self.assert_incomplete(self.observe())

    def test_codex_retired_session_is_unavailable(self):
        self.use_codex()
        with closing(codex.connect(self.native)) as conn: conn.execute("UPDATE codex_sessions SET status='retired'")
        self.assertFalse(self.observe().session_available)

    def test_readonly_factory_works_with_row_factory_and_never_changes_database_bytes(self):
        self.use_codex(); self.codex_job()
        self.observer._codex = lambda: codex.connect(self.native)
        before = {path:path.read_bytes() for path in (self.h.config_path,Path(self.h.path),self.native)}
        self.observe()
        for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)

    def test_selected_version_binding_uses_version_jobs_and_rejects_child_as_root(self):
        config = self.h.get('legacy'); row = dict(config['sessions'][0]); row['id'] = 'synthetic_child'
        config['sessions'].append(row); self.h.put('legacy',config)
        self.h.conn.execute("INSERT INTO web_message_versions(version_id,root_id) VALUES (?,?)", ('synthetic_child',CID))
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)', (CID,'synthetic_child'))
        self.api.accept(CID, 'synthetic root', {'api_session':CID})
        self.api.accept('synthetic_child', 'synthetic version', {'api_session':'synthetic_child'})
        result = self.observe()
        self.assertEqual(result.version_id,'synthetic_child'); self.assertEqual(result.active_jobs,1)
        self.rejects(lambda: self.observer.observe('synthetic_child'), 'lifecycle_conversation_root_required')

    def test_query_only_blocks_accidental_writes_on_rw_factory(self):
        with self.observer._read(self.h.connect_canonical) as conn:
            with self.assertRaises(sqlite3.OperationalError): conn.execute("DELETE FROM messages")

    def test_store_error_does_not_leak_path_or_config_secrets(self):
        self.observer._relay = readonly_factory(self.root/'missing-secret.sqlite')
        self.rejects(self.observe, 'observation_store_unavailable')


if __name__ == '__main__': unittest.main()
