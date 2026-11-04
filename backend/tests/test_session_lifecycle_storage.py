"""Real canonical messages DDL/connect + separate JSON, synthetic inputs only."""
from contextlib import closing
from dataclasses import replace
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

from backend import channel_store, deployment_config, api_web_generation
from backend.session_lifecycle import LifecycleError, SessionLifecycle
from backend.session_lifecycle_codec import MAX_BYTES, checksum, decode_record, encode_record
from backend.session_lifecycle_storage import (
    JsonSessionReader, SQLiteLifecycleRepository, install_schema, rollback_empty_schema,
)
from backend.tests.test_session_lifecycle import Harness, CID, CTX, ACTIVE


class FileHarness(Harness):
    def __init__(self, path, *, seed=True):
        self.config_path = Path(path).with_suffix('.json')
        self.connection_wrapper = None
        super().__init__(path, seed=seed)
        if seed:
            # Only a disposable fixture is converted to the actual canonical DDL.
            self.conn.execute('ALTER TABLE messages RENAME TO fixture_old_messages')
            self.conn.execute(channel_store.RELAY_TABLE_DDL['messages'])
            self.conn.execute("INSERT INTO messages(id,ts,kind,direction,text,meta) SELECT id,"
                              "'2026-11-04T00:00:00Z',kind,direction,text,meta FROM fixture_old_messages")
            self.conn.execute('DROP TABLE fixture_old_messages')
        self.reader = JsonSessionReader(self.config_path, historical_provider=lambda _: None)
        with closing(self.connect_canonical()) as conn:
            install_schema(conn)
        self.repo = SQLiteLifecycleRepository(self.connect_canonical, self.reader, enabled=True)
        self.engine.repository = self.repo

    def get(self, key):
        if key == 'legacy':
            return json.loads(self.config_path.read_text())
        return super().get(key)

    def put(self, key, value):
        if key == 'legacy':
            deployment_config.atomic_write_text(self.config_path, json.dumps(value))
        else:
            super().put(key, value)

    def connect_canonical(self):
        conn = channel_store.connect(self.path)
        return self.connection_wrapper(conn) if self.connection_wrapper else conn

    def record(self):
        row = self.conn.execute('SELECT payload FROM session_lifecycle_records WHERE conversation_id=?', (CID,)).fetchone()
        return decode_record(row[0]) if row else None


class Proxy:
    def __init__(self, conn, *, before=None, after=None, execute_hook=None):
        self.conn, self.before, self.after, self.execute_hook = conn, before, after, execute_hook

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if self.execute_hook: self.execute_hook(sql)
        return self.conn.execute(sql, *args)

    def commit(self):
        if self.before: self.before()
        self.conn.commit()
        if self.after: self.after()


def file_restart_worker(path):
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')):
        h = FileHarness(path, seed=False)
        receipt = h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)
        packet = h.engine.packet(CID, access=CTX)
        print(json.dumps({'phase':h.record().phase, 'replayed':receipt.replayed,
                          'conversation':packet.conversation_id, 'pending':list(packet.pending)}))
        h.conn.close()


@unittest.skipUnless(os.name == 'posix', 'private JSON reader is a POSIX candidate')
class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.h = FileHarness(self.root/'relay.sqlite')
        self.addCleanup(lambda: self.h.conn.close())
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')))

    def closeout(self):
        return self.h.engine.closeout(CID, 'close_1', window_id='window_0', context_epoch=0,
                                      active_state=ACTIVE, access=CTX)

    def prepare(self):
        self.closeout()
        return self.h.engine.reentry(CID, 'prepare_1', closeout_id='close_1', target_window_id='window_1', access=CTX)

    def rejects(self, fn, category):
        with self.assertRaises(LifecycleError) as caught: fn()
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(str(caught.exception), category)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn('synthetic secret', str(caught.exception))

    def test_default_disabled_does_not_open_connection_or_read_config(self):
        def forbidden(): raise AssertionError('must not open')
        repo = SQLiteLifecycleRepository(forbidden, None)
        def run():
            with repo.transaction(CID): pass
        self.rejects(run, 'lifecycle_disabled')

    def test_roundtrip_engine_uses_real_repository_and_keeps_canonical_rows(self):
        before = self.h.conn.execute('SELECT * FROM messages').fetchall()
        self.prepare(); self.h.confirm()
        state = self.h.record()
        result = self.h.engine.activate(CID, 'activate_1', packet_sha256=state.packet_sha256, access=CTX)
        self.assertTrue(result.window_confirmed)
        self.assertEqual((self.h.record().conversation_id,self.h.record().window_id), (CID,'window_1'))
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(), before)
        self.assertEqual(len(self.h.session_authority.session_rows()), 1)
        self.assertEqual(self.h.conn.execute('SELECT COUNT(*) FROM session_lifecycle_receipts').fetchone()[0], 3)

    def test_codec_unknown_version_duplicate_fields_and_boolean_revision_rejected(self):
        self.closeout()
        raw = self.h.conn.execute('SELECT payload FROM session_lifecycle_records').fetchone()[0]
        obj = json.loads(raw)
        variants = []
        for version in (0, 2, True):
            copy = json.loads(raw); copy['schema_version'] = version
            variants.append(json.dumps(copy,sort_keys=True,separators=(',',':')))
        copy = json.loads(raw); copy['data']['revision'] = True
        variants.append(json.dumps(copy,sort_keys=True,separators=(',',':')))
        variants.append(raw.replace('"schema_version":1','"schema_version":1,"schema_version":1'))
        for variant in variants:
            self.h.conn.execute('UPDATE session_lifecycle_records SET payload=?,sha256=?', (variant,checksum(variant)))
            self.rejects(self.closeout, 'lifecycle_storage_corrupt')
        self.h.conn.execute('UPDATE session_lifecycle_records SET payload=?,sha256=?', (raw,checksum(raw)))

    def test_checksum_detects_payload_damage_but_is_not_a_signature(self):
        self.closeout()
        self.h.conn.execute("UPDATE session_lifecycle_records SET sha256=?", ('0'*64,))
        self.rejects(self.closeout, 'lifecycle_storage_corrupt')

    def test_oversized_stored_text_is_rejected_before_python_text_decoding(self):
        self.closeout()
        raw = 'x' * (MAX_BYTES + 1)
        self.h.conn.execute('UPDATE session_lifecycle_records SET payload=?,sha256=?', (raw,checksum(raw)))
        original = self.h.connect_canonical
        def factory():
            conn = original()
            def text_factory(data):
                if len(data) > MAX_BYTES: raise AssertionError('unbounded payload reached decoder')
                return data.decode('utf-8')
            conn.text_factory = text_factory
            return conn
        self.h.repo._connect = factory
        self.rejects(self.closeout, 'lifecycle_storage_corrupt')

    def test_codec_rejects_out_of_order_message_refs_and_oversized_state(self):
        self.prepare(); state = self.h.record()
        self.rejects(lambda: encode_record(replace(state,message_refs=(2,1),message_cursor=2)), 'lifecycle_storage_corrupt')
        self.rejects(lambda: encode_record(replace(state,summary_candidates=('x'*2049,))), 'lifecycle_storage_corrupt')

    def test_receipt_corruption_is_checked_before_idempotent_replay(self):
        self.closeout()
        raw = self.h.conn.execute('SELECT payload FROM session_lifecycle_receipts').fetchone()[0]
        obj = json.loads(raw); obj['data']['reply_completed'] = True
        broken = json.dumps(obj,sort_keys=True,separators=(',',':'))
        self.h.conn.execute('UPDATE session_lifecycle_receipts SET payload=?,sha256=?', (broken,checksum(broken)))
        self.rejects(self.closeout, 'lifecycle_storage_corrupt')

    def test_install_is_explicit_idempotent_and_empty_rollback_preserves_messages(self):
        before = self.h.conn.execute('SELECT * FROM messages').fetchall()
        with closing(self.h.connect_canonical()) as conn:
            self.assertFalse(install_schema(conn))
            rollback_empty_schema(conn)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='session_lifecycle_records'").fetchone())
            self.assertTrue(install_schema(conn))
        self.assertEqual(self.h.conn.execute('SELECT * FROM messages').fetchall(), before)

    def test_populated_schema_cannot_be_dropped_by_rollback(self):
        self.closeout()
        with closing(self.h.connect_canonical()) as conn:
            self.rejects(lambda: rollback_empty_schema(conn), 'lifecycle_rollback_requires_empty')
        self.assertEqual(self.h.record().revision, 1)

    def test_wrong_relay_and_owned_schema_fail_before_writes(self):
        with closing(sqlite3.connect(self.root/'not-relay.sqlite',isolation_level=None)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            self.rejects(lambda: install_schema(conn), 'lifecycle_relay_schema_invalid')
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'session_lifecycle_%'").fetchone())
        self.h.conn.execute('UPDATE session_lifecycle_meta SET codec_version=2')
        self.rejects(self.closeout, 'lifecycle_schema_invalid')

    def test_trigger_on_owned_tables_is_rejected_without_running_it(self):
        self.h.conn.execute('CREATE TRIGGER dangerous_lifecycle AFTER INSERT ON session_lifecycle_records '
                            'BEGIN DELETE FROM messages; END')
        self.rejects(self.closeout, 'lifecycle_schema_invalid')
        self.assertEqual(self.h.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)

    def test_migration_does_not_commit_a_callers_existing_transaction(self):
        with closing(self.h.connect_canonical()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.rejects(lambda: install_schema(conn), 'lifecycle_connection_invalid')
            self.assertTrue(conn.in_transaction)
            conn.rollback()

    def test_disabled_foreign_keys_connection_is_rejected(self):
        def factory():
            conn = self.h.connect_canonical(); conn.execute('PRAGMA foreign_keys=OFF'); return conn
        self.h.repo._connect = factory
        self.rejects(self.closeout, 'lifecycle_connection_invalid')

    def test_raw_config_secrets_are_not_in_view_state_or_receipts(self):
        config = self.h.get('legacy'); config['main_chain'] = [{'key':'synthetic secret never export'}]
        self.h.put('legacy', config)
        view = self.h.reader.capture(CID)
        self.assertNotIn('synthetic secret', repr(view))
        self.closeout()
        raw = self.h.conn.execute('SELECT payload FROM session_lifecycle_records').fetchone()[0]
        self.assertNotIn('synthetic secret', raw)

    def test_duplicate_config_keys_symlinks_and_public_permissions_rejected(self):
        original = self.h.config_path.read_text()
        self.h.config_path.write_text('{"sessions":[],"sessions":[]}')
        self.rejects(lambda: self.h.reader.capture(CID), 'lifecycle_config_invalid')
        self.h.config_path.write_text(original)
        self.h.config_path.chmod(0o644)
        self.rejects(lambda: self.h.reader.capture(CID), 'lifecycle_config_invalid')
        self.h.config_path.chmod(0o600)
        link = self.root/'config-link'; link.symlink_to(self.h.config_path)
        reader = JsonSessionReader(link,historical_provider=lambda _:None)
        self.rejects(lambda: reader.capture(CID), 'lifecycle_config_invalid')

    def test_unknown_or_deleted_config_session_never_falls_back_to_api(self):
        self.rejects(lambda: self.h.reader.capture('unknown'), 'lifecycle_session_unavailable')
        config = self.h.get('legacy'); config['sessions'] = []
        config['deleted_sessions'] = [{'id':CID,'provider':'api','deleted_at':'2026-11-04T01:00:00Z'}]
        self.h.put('legacy', config)
        self.rejects(lambda: self.h.reader.capture(CID), 'lifecycle_session_unavailable')

    def test_observed_atomic_config_replacement_before_commit_rolls_back_sql(self):
        parent = self.h.reader
        harness = self.h
        class ChangedReader:
            def capture(self,cid): return parent.capture(cid)
            def is_current(self,view):
                config = harness.get('legacy'); config['sessions'][0]['title']='synthetic changed'
                harness.put('legacy',config)
                return parent.is_current(view)
        self.h.repo._reader = ChangedReader()
        self.rejects(self.closeout, 'lifecycle_authority_changed')
        self.assertIsNone(self.h.record())
        self.assertEqual(self.h.conn.execute('SELECT COUNT(*) FROM session_lifecycle_receipts').fetchone()[0], 0)

    def test_post_commit_config_conflict_reports_uncertainty_and_keeps_receipt(self):
        def after():
            config = self.h.get('legacy'); config['sessions'][0]['title']='synthetic after commit'
            self.h.put('legacy', config)
        self.h.connection_wrapper = lambda conn: Proxy(conn,after=after)
        self.rejects(self.closeout, 'lifecycle_authority_changed_after_commit')
        self.assertEqual(self.h.record().revision, 1)
        self.h.connection_wrapper = None
        self.assertTrue(self.closeout().replayed)

    def test_two_file_checks_do_not_prove_exclusion_of_uncooperative_writer(self):
        parent, harness = self.h.reader, self.h
        original_view = parent.capture(CID)
        class LateWriter:
            calls = 0
            def capture(self,cid): return parent.capture(cid)
            def is_current(self,view):
                current = parent.is_current(view); self.calls += 1
                if self.calls == 2:
                    config = harness.get('legacy'); config['sessions'][0]['title']='synthetic racing writer'
                    harness.put('legacy',config)  # After the final observation.
                return current
        self.h.repo._reader = LateWriter()
        result = self.closeout()
        self.assertFalse(result.replayed)
        self.assertFalse(parent.is_current(original_view))
        self.assertFalse(self.h.repo.cross_store_atomic)
        self.assertFalse(self.h.repo.production_supported)
        # This deliberate counterexample is a production gate, not a PASS claim.

    def test_state_and_receipt_rollback_together_on_mid_save_failure(self):
        def hook(sql):
            if sql.startswith('INSERT INTO session_lifecycle_receipts'):
                raise sqlite3.OperationalError('synthetic private SQL failure')
        self.h.connection_wrapper = lambda conn: Proxy(conn,execute_hook=hook)
        self.rejects(self.closeout, 'lifecycle_storage_unavailable')
        self.assertIsNone(self.h.record())
        self.assertEqual(self.h.conn.execute('SELECT COUNT(*) FROM session_lifecycle_receipts').fetchone()[0], 0)

    def test_lost_commit_ack_reconciles_once_using_real_repository(self):
        def after(): raise OSError('synthetic lost commit ack')
        self.h.connection_wrapper = lambda conn: Proxy(conn,after=after)
        self.rejects(self.closeout, 'lifecycle_storage_unavailable')
        self.h.connection_wrapper = None
        self.assertTrue(self.closeout().replayed)
        self.assertEqual(self.h.record().revision, 1)

    def test_dangling_or_cross_session_canonical_message_reference_is_rejected(self):
        self.prepare()
        self.h.conn.execute("UPDATE messages SET meta=? WHERE id=1", (json.dumps({'api_session':'another_session'}),))
        def load():
            with self.h.repo.transaction(CID) as tx: tx.load()
        self.rejects(load, 'lifecycle_message_scope_changed')
        self.h.conn.execute('DELETE FROM messages WHERE id=1')
        self.rejects(load, 'lifecycle_message_scope_changed')

    def test_current_config_provider_mismatch_rejected_before_stored_receipt(self):
        self.closeout()
        config = self.h.get('legacy'); config['sessions'][0]['provider']='codex'; self.h.put('legacy',config)
        self.rejects(self.closeout, 'lifecycle_session_changed')

    def test_alternative_cannot_be_a_new_logical_root_and_selection_change_blocks_replay(self):
        self.closeout()
        config = self.h.get('legacy')
        config['sessions'].append({**config['sessions'][0], 'id':'synthetic_alternative'})
        self.h.put('legacy',config)
        self.h.conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)',
                            ('synthetic_alternative',CID,CID,'regenerate',1,1,1))
        def alternative():
            with self.h.repo.transaction('synthetic_alternative') as tx: tx.load()
        self.rejects(alternative,'lifecycle_conversation_root_required')
        self.h.conn.execute('INSERT INTO web_version_selections VALUES (?,?)',(CID,'synthetic_alternative'))
        def original():
            with self.h.repo.transaction(CID) as tx: tx.load()
        self.rejects(original,'lifecycle_session_changed')

    def test_actual_api_store_writer_is_blocked_by_repository_sql_transaction(self):
        store = api_web_generation.Store(self.h.path); store.initialize()
        connect = sqlite3.connect
        def short_timeout(*args,**kwargs):
            kwargs['timeout']=.02
            return connect(*args,**kwargs)
        with self.h.repo.transaction(CID):
            with patch('backend.api_web_generation.sqlite3.connect',side_effect=short_timeout):
                with self.assertRaises(sqlite3.OperationalError):
                    store.accept(CID,'synthetic API input',{'api_session':CID})
        result = store.accept(CID,'synthetic API input',{'api_session':CID})
        self.assertEqual(result['kind'],'user')
        self.assertEqual(self.h.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],2)

    def test_transaction_object_cannot_be_used_after_commit(self):
        with self.h.repo.transaction(CID) as tx:
            self.assertIsNone(tx.load())
        self.rejects(tx.load, 'lifecycle_transaction_closed')

    def test_new_process_reopens_json_and_same_real_canonical_repository(self):
        self.closeout(); self.h.conn.close()
        command = [sys.executable,'-c','import sys; from backend.tests.test_session_lifecycle_storage import file_restart_worker; file_restart_worker(sys.argv[1])',self.h.path]
        first = subprocess.run(command,check=True,capture_output=True,text=True,timeout=15)
        second = subprocess.run(command,check=True,capture_output=True,text=True,timeout=15)
        self.h = FileHarness(self.h.path,seed=False)
        self.assertFalse(json.loads(first.stdout)['replayed']); self.assertTrue(json.loads(second.stdout)['replayed'])
        self.assertEqual(json.loads(second.stdout)['conversation'],CID)
        self.assertEqual(json.loads(second.stdout)['pending'],['task_1'])

    def test_rollback_and_wal_fixture_modes_are_preserved(self):
        initial = self.h.conn.execute('PRAGMA journal_mode').fetchone()[0]
        self.closeout()
        self.assertEqual(self.h.conn.execute('PRAGMA journal_mode').fetchone()[0],initial)
        self.h.conn.close()
        self.h = FileHarness(self.root/'wal-relay.sqlite')
        self.assertEqual(self.h.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        self.closeout()
        self.assertEqual(self.h.conn.execute('PRAGMA journal_mode').fetchone()[0],'wal')


if __name__ == '__main__': unittest.main()
