"""Disposable SQLite/WAL recovery only; no production files, SDK or networking."""
from contextlib import closing,redirect_stdout,redirect_stderr
import copy
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.usage_ledger import Ledger,LedgerError,build_view
from backend.usage_ledger_backup import snapshot,isolated_restore,mock_rehearsal,main
from backend.usage_ledger_continuation import Continuations
from backend.usage_ledger_storage import page,maintain
from backend.tests.test_usage_ledger import record
from backend.tests import test_usage_ledger_continuation as continuation_fixture
from backend.tests import test_usage_ledger_composition as composition_fixture


class BackupTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.source=self.root/'source.sqlite';self.target=self.root/'snapshot.sqlite'
        self.ledger=Ledger(self.source)
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline-only')))

    def test_mock_rehearsal_never_claims_production_or_native_continuity_acceptance(self):
        value=mock_rehearsal()
        self.assertTrue(value['ok']);self.assertTrue(all(value['checks'].values()))
        for k in ('production_verified','enables_production','production_restore_supported',
                  'external_thread_exclusivity_verified','native_notification_continuity_verified'):
            self.assertIs(value[k],False)
        self.assertEqual(value['basis'],'disposable_mock_only')

    def test_backup_includes_uncheckpointed_commits_without_changing_source_main_or_wal(self):
        with closing(self.ledger.connect()) as reader:
            reader.execute('BEGIN');reader.execute('SELECT COUNT(*) FROM usage_ledger').fetchone()
            self.ledger.append(record())
            wal=Path(str(self.source)+'-wal');self.assertGreater(wal.stat().st_size,0)
            before=(self.source.read_bytes(),wal.read_bytes())
            snapshot(self.source,self.target)
            self.assertEqual((self.source.read_bytes(),wal.read_bytes()),before)
            with sqlite3.connect(self.target) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_ledger').fetchone()[0],1)
                self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertFalse(Path(str(self.target)+'-wal').exists())
            self.assertFalse(Path(str(self.target)+'-shm').exists())

    def test_snapshot_is_new_private_standalone_file_and_result_does_not_expose_payloads(self):
        self.ledger.append(record())
        value=snapshot(self.source,self.target)
        if os.name=='posix':self.assertEqual(self.target.stat().st_mode&0o777,0o600)
        text=json.dumps(value)
        for private in (str(self.root),'provider-a','s-1','cursor_key','raw_usage'):
            self.assertNotIn(private,text)
        self.assertEqual(value['scope'],'dedicated_ledger_only')

    def test_existing_targets_sources_and_aliases_are_never_overwritten(self):
        self.target.write_bytes(b'synthetic existing file')
        for target in (self.target,self.source):
            before=target.read_bytes()
            with self.assertRaisesRegex(LedgerError,'^ledger_backup_unavailable$'):snapshot(self.source,target)
            self.assertEqual(target.read_bytes(),before)
        link=self.root/'hardlink';os.link(self.source,link)
        with self.assertRaises(LedgerError):snapshot(self.source,link)
        link.unlink();link.symlink_to(self.source)
        with self.assertRaises(LedgerError):snapshot(self.source,link)
        self.assertTrue(link.is_symlink())

    def test_source_and_destination_sidecar_names_are_reserved(self):
        for suffix in ('-wal','-shm','-journal'):
            with self.assertRaises(LedgerError):snapshot(self.source,Path(str(self.source)+suffix))
            sidecar=Path(str(self.target)+suffix);sidecar.write_bytes(b'synthetic reserved bytes')
            with self.assertRaises(LedgerError):snapshot(self.source,self.target)
            self.assertEqual(sidecar.read_bytes(),b'synthetic reserved bytes');sidecar.unlink()
        self.assertFalse(self.target.exists())

    def test_missing_corrupt_or_unrelated_sources_cannot_create_a_backup(self):
        missing=self.root/'missing.sqlite';corrupt=self.root/'corrupt.sqlite';corrupt.write_bytes(b'not sqlite')
        unrelated=self.root/'unrelated.sqlite'
        with sqlite3.connect(unrelated) as conn:conn.execute('CREATE TABLE messages(text TEXT)')
        for source in (missing,corrupt,unrelated):
            with self.assertRaisesRegex(LedgerError,'^ledger_backup_unavailable$'):snapshot(source,self.target)
            self.assertFalse(self.target.exists())
        self.assertFalse(missing.exists())

    def test_mixed_chat_or_memory_database_is_rejected_even_with_ledger_tables(self):
        for name in ('messages','memory_items'):
            with closing(self.ledger.connect()) as conn,conn:conn.execute('CREATE TABLE '+name+'(payload TEXT)')
            with self.assertRaises(LedgerError):snapshot(self.source,self.target)
            self.assertFalse(self.target.exists())
            with closing(self.ledger.connect()) as conn,conn:conn.execute('DROP TABLE '+name)

    def test_source_symlink_and_shared_destination_directory_are_rejected(self):
        link=self.root/'source-link';link.symlink_to(self.source)
        with self.assertRaises(LedgerError):snapshot(link,self.target)
        shared=self.root/'shared';shared.mkdir(mode=0o755);shared.chmod(0o755)
        with self.assertRaises(LedgerError):snapshot(self.source,shared/'backup.sqlite')
        self.assertFalse((shared/'backup.sqlite').exists())

    def test_backup_is_frozen_and_cannot_reprice_or_absorb_later_requests(self):
        self.ledger.append(record());expected=self.ledger.records('api')
        snapshot(self.source,self.target)
        price=copy.deepcopy(record()['price']);price['per_million']['input']='99'
        self.ledger.append(record(request_id='later',price=price))
        with isolated_restore(self.target) as target:
            self.assertEqual(Ledger(target).records('api'),expected)
        self.assertEqual(len(self.ledger.records('api')),2)

    def test_cursor_settings_and_retention_survive_restoration_without_resetting_revision(self):
        for n in range(3):self.ledger.append(record(request_id=str(n),turn_id=str(n)))
        self.ledger.save_settings('api',0,{'budgets':[{'period':'day','currency':'USD','limit':'4'}]})
        maintain(self.ledger,'api',raw_before='2026-10-24T00:00:00Z')
        self.ledger.purge_session('retired',retire=True,mode='api')
        first=page(self.ledger,'api','s-1',limit=1);before=build_view(self.ledger,'api','s-1')['summary']
        snapshot(self.source,self.target)
        with isolated_restore(self.target) as target:
            restored=Ledger(target)
            self.assertEqual(build_view(restored,'api','s-1')['summary'],before)
            self.assertEqual(restored.settings('api'),self.ledger.settings('api'))
            self.assertEqual(len(page(restored,'api','s-1',limit=1,cursor=first['next_cursor'])['records']),1)
            self.assertTrue(all(r['raw_usage'] is None for r in restored.records('api')))
            self.assertFalse(restored.append(record(session_id='retired',request_id='late')))

    def test_isolated_restore_always_uses_a_fresh_temporary_directory_then_removes_it(self):
        snapshot(self.source,self.target);saved=self.target.read_bytes()
        with isolated_restore(self.target) as first:self.assertTrue(first.is_file())
        self.assertFalse(first.exists())
        with isolated_restore(self.target) as second:self.assertNotEqual(first,second)
        self.assertEqual(self.target.read_bytes(),saved)

    def test_older_backup_cannot_know_later_deletions_and_is_never_promoted_to_live(self):
        self.ledger.append(record());snapshot(self.source,self.target)
        self.ledger.purge_session('s-1',retire=True,mode='api')
        with isolated_restore(self.target) as target:
            restored=Ledger(target)
            self.assertEqual(len(restored.records('api')),1)
            # This deliberately proves the stale-snapshot boundary, in a disposable copy only.
            self.assertTrue(restored.append(record(request_id='isolated-only-late')))
        self.assertEqual(self.ledger.records('api'),[])
        self.assertFalse(self.ledger.append(record(request_id='source-still-retired')))

    def test_deadline_aborts_partial_backup_without_deleting_existing_files(self):
        self.ledger.append(record());before=self.source.read_bytes();calls=[]
        def clock():calls.append(1);return 0 if len(calls)<3 else 100
        with patch('backend.usage_ledger_backup.time',SimpleNamespace(monotonic=clock)):
            with self.assertRaisesRegex(LedgerError,'^ledger_backup_unavailable$'):snapshot(self.source,self.target)
        self.assertFalse(self.target.exists());self.assertEqual(self.source.read_bytes(),before)

    def test_timeout_bounds_and_invalid_paths_fail_before_sqlite_is_opened(self):
        with patch('sqlite3.connect',side_effect=AssertionError('must reject without opening SQLite')):
            for timeout in (0,-1,31,True,float('nan'),float('inf'),'10'):
                with self.assertRaises(LedgerError):snapshot(self.source,self.target,timeout_seconds=timeout)
            for value in ('relative.sqlite',':memory:',self.root/'..'/'private.sqlite',object()):
                with self.assertRaises(LedgerError):snapshot(value,self.target)

    def test_continuation_receipt_hmac_binding_is_preserved_but_external_key_is_still_required(self):
        fixture=continuation_fixture.ContinuationTests('test_new_target_is_observed_not_successful_or_draft_delivered')
        fixture.setUp();self.addCleanup(fixture.doCleanups);fixture.register()
        snapshot(fixture.ledger.path,self.target)
        with isolated_restore(self.target) as target:
            restored=Ledger(target)
            kwargs={'relay_db':fixture.relay,'native_db':fixture.native,'session_row':fixture.rows.get}
            adapter=Continuations(restored,digest_key=fixture.adapter.key,**kwargs)
            args={'source_id':'source','target_id':'target','mode':'api','request_id':fixture.request,
                  'expected_text':'synthetic editable draft'}
            self.assertFalse(adapter.register(**args))
            wrong=Continuations(restored,digest_key=b'different-synthetic-runtime-key-only',**kwargs)
            with self.assertRaisesRegex(LedgerError,'continuation_conflict'):wrong.register(**args)

    def test_cli_is_mock_only_and_runtime_errors_are_fixed_not_original_exceptions(self):
        out=io.StringIO()
        with redirect_stdout(out):self.assertEqual(main(['--mock']),0)
        self.assertTrue(json.loads(out.getvalue())['ok'])
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            main(['--mock','--source','/synthetic-private-file'])
        out=io.StringIO()
        with patch('backend.usage_ledger_backup.mock_rehearsal',side_effect=OSError('synthetic private path')):
            with redirect_stdout(out):self.assertEqual(main(['--mock']),1)
        self.assertNotIn('synthetic private',out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['error'],'ledger_backup_unavailable')

    def test_recovered_native_ledger_keeps_history_without_reusing_old_runtime_baselines(self):
        from backend.usage_ledger_codex import CodexCollector
        class Fixture(unittest.TestCase):
            setUp=composition_fixture.CompositionFixture.setUp
            bundle=composition_fixture.CompositionFixture.bundle
            seed=composition_fixture.CompositionFixture.seed
            emit=composition_fixture.CompositionFixture.emit
        fixture=Fixture('setUp')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        services=fixture.bundle(capture=True)
        for n in (1,2):fixture.seed(n);fixture.emit(services,n)
        self.assertEqual(services.ledger.records('codex')[-1]['normalized']['metrics']['input'],100)
        snapshot(services.ledger.path,self.target)
        with isolated_restore(self.target) as target:
            restored=Ledger(target)
            collector=CodexCollector(restored,relay_db=fixture.relay,store_path=fixture.native,
                                     provider_id='synthetic-subscription',exclusive_threads=True)
            recovered=SimpleNamespace(codex=collector)
            fixture.seed(3);fixture.emit(recovered,3)
            self.assertEqual(len(restored.records('codex')),3)
            self.assertIsNone(restored.records('codex')[-1]['usage_baseline'])
            fixture.seed(4);fixture.emit(recovered,4)
            self.assertEqual(restored.records('codex')[-1]['normalized']['metrics']['input'],100)
