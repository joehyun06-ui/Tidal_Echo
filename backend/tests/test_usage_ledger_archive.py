"""Real SQLite + AEAD with synthetic rows; all network access denied."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import usage_ledger_archive as archive
from backend.usage_ledger import Ledger, LedgerError
from backend.usage_ledger_backup import snapshot, mock_rehearsal as original_rehearsal


BASE = {'mode': 'api', 'provider_id': 'synthetic-api', 'request_id': 'one',
        'turn_id': 'one', 'session_id': 'synthetic-session', 'at': '2026-10-23T02:00:00Z',
        'model': 'synthetic-model', 'dialect': 'openai_responses',
        'raw_usage': {'input_tokens': 10, 'output_tokens': 2},
        'usage_complete': True, 'status': 'completed'}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source.sqlite'
        self.frozen = self.root / 'snapshot.sqlite'
        self.ledger = Ledger(self.source)
        self.ledger.append(BASE)
        snapshot(self.source, self.frozen)
        self.key = AESGCM.generate_key(bit_length=256)
        self.key_id = 'synthetic-key-v1'
        self.blob, self.schema = archive._seal(self.frozen, self.key, self.key_id)
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline')))

    def recover(self, blob=None, **kwargs):
        return archive._recover(self.blob if blob is None else blob, self.key, self.key_id,
                                self.schema, authorized=True, **kwargs)

    def forged(self, change):
        manifest, data = archive._decode(self.blob, self.key, self.key_id, self.schema)
        result = change(manifest, data)
        if result is not None:
            data = result
        metadata = archive._json(manifest)
        nonce = os.urandom(12)
        plain = struct.pack('>I', len(metadata)) + metadata + data
        return archive.MAGIC + nonce + AESGCM(self.key).encrypt(nonce, plain, archive._aad(self.key, self.key_id))

    def denied(self, blob, **kwargs):
        with self.assertRaisesRegex(LedgerError, '^ledger_archive_unavailable$'):
            with self.recover(blob, **kwargs):
                self.fail('must not yield a database')

    def test_disk_reopen_then_isolated_restore_is_byte_exact_and_private(self):
        path = archive._publish(self.root, self.blob)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.recover(archive._read_archive(path)) as restored:
            self.assertEqual(restored.read_bytes(), self.frozen.read_bytes())
            self.assertEqual(restored.stat().st_mode & 0o777, 0o600)
            self.assertEqual(restored.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotEqual(restored.parent, self.root)
        self.assertFalse(restored.exists())

    def test_original_backup_rehearsal_still_passes(self):
        result = original_rehearsal()
        self.assertTrue(result['ok'])
        self.assertTrue(all(result['checks'].values()))

    def test_new_rehearsal_preserves_wal_cursor_retention_and_hold(self):
        value = archive.mock_rehearsal()
        self.assertTrue(value['ok'])
        self.assertTrue(all(value['checks'].values()))
        self.assertEqual(value['production'], 'HOLD')
        for key in ('offsite_verified', 'power_loss_verified', 'production_backup_created',
                    'production_restore_verified', 'enables_production'):
            self.assertIs(value[key], False)

    def test_wrong_key_and_key_id_fail_before_plaintext_file_creation(self):
        for key, key_id in ((b'x' * 32, self.key_id), (self.key, 'different-key-id')):
            with self.subTest(key_id=key_id), patch.object(archive, '_write_new', side_effect=AssertionError('no plaintext')):
                with self.assertRaisesRegex(LedgerError, '^ledger_archive_unavailable$'):
                    with archive._recover(self.blob, key, key_id, self.schema, authorized=True):
                        self.fail()

    def test_no_restore_without_explicit_mock_authorization(self):
        for flag in (False, None, 1, 'yes'):
            with self.subTest(flag=flag), self.assertRaises(LedgerError):
                with archive._recover(self.blob, self.key, self.key_id, self.schema, authorized=flag):
                    self.fail()

    def test_magic_nonce_ciphertext_and_tag_tampering_rejected(self):
        for index in (0, len(archive.MAGIC), len(archive.MAGIC) + 12, len(self.blob) - 1):
            changed = bytearray(self.blob)
            changed[index] ^= 1
            with self.subTest(index=index):
                self.denied(bytes(changed))

    def test_partial_empty_and_appended_archives_rejected(self):
        for blob in (b'', self.blob[:8], self.blob[:-1], self.blob[:len(self.blob)//2], self.blob + b'junk'):
            self.denied(blob)

    def test_wrong_expected_schema_rejected_before_writing(self):
        with patch.object(archive, '_write_new', side_effect=AssertionError('no plaintext')):
            with self.assertRaises(LedgerError):
                with archive._recover(self.blob, self.key, self.key_id, '0' * 64, authorized=True):
                    self.fail()

    def test_unknown_old_and_boolean_archive_versions_rejected(self):
        for version in (0, 2, True, '1'):
            with self.subTest(version=version):
                self.denied(self.forged(lambda m, d: m.update(archive_version=version)))

    def test_missing_extra_and_wrong_scope_manifest_fields_rejected(self):
        changes = (lambda m, d: m.pop('scope') and None,
                   lambda m, d: m.update(extra='unsupported'),
                   lambda m, d: m.update(scope='all_chat_databases'))
        for change in changes:
            self.denied(self.forged(change))

    def test_snapshot_hash_length_and_counts_mismatch_rejected(self):
        changes = (lambda m, d: m.update(snapshot_sha256='0' * 64),
                   lambda m, d: m.update(snapshot_size=m['snapshot_size'] + 1),
                   lambda m, d: m['table_counts'].update(usage_ledger=999))
        for change in changes:
            self.denied(self.forged(change))

    def test_manifest_boolean_counts_and_size_rejected(self):
        for change in (lambda m, d: m.update(snapshot_size=True),
                       lambda m, d: m['table_counts'].update(usage_ledger=True)):
            self.denied(self.forged(change))

    def test_corrupt_sqlite_even_with_authenticated_matching_hash_is_rejected(self):
        def corrupt(manifest, data):
            changed = b'not-sqlite' + data[10:]
            manifest['snapshot_sha256'] = hashlib.sha256(changed).hexdigest()
            return changed
        self.denied(self.forged(corrupt))

    def test_authenticated_row_corruption_fails_sqlite_integrity(self):
        def corrupt(manifest, data):
            changed = bytearray(data)
            changed[100] = 0  # Invalid b-tree page type, keeping the SQLite header.
            changed = bytes(changed)
            manifest['snapshot_sha256'] = hashlib.sha256(changed).hexdigest()
            return changed
        self.denied(self.forged(corrupt))

    def test_schema_change_not_accepted_by_old_restore_contract(self):
        with closing(sqlite3.connect(self.frozen)) as conn, conn:
            conn.execute('CREATE TABLE usage_ledger_future(value TEXT)')
        changed, _ = archive._seal(self.frozen, self.key, self.key_id)
        self.denied(changed)

    def test_application_user_version_change_is_also_a_compatibility_boundary(self):
        with closing(sqlite3.connect(self.frozen)) as conn:
            conn.execute('PRAGMA user_version=7')
        changed, _ = archive._seal(self.frozen, self.key, self.key_id)
        self.denied(changed)

    def test_invalid_timestamp_and_sqlite_version_metadata_rejected(self):
        for key, value in (('created_at', {}), ('created_at', 'invalid-date'),
                           ('created_at', '2026-11-04T10:00:00'),
                           ('sqlite_version', 123), ('sqlite_version', 'unknown')):
            with self.subTest(key=key, value=value):
                self.denied(self.forged(lambda m, d: m.update({key: value})))

    def test_nonledger_table_in_source_is_rejected(self):
        with closing(sqlite3.connect(self.frozen)) as conn, conn:
            conn.execute('CREATE TABLE messages(text TEXT)')
        with self.assertRaises(LedgerError):
            archive._seal(self.frozen, self.key, self.key_id)

    def test_foreign_key_violation_is_rejected(self):
        with closing(sqlite3.connect(self.frozen)) as conn, conn:
            conn.execute('CREATE TABLE usage_ledger_child(k TEXT REFERENCES usage_ledger_meta(key))')
            conn.execute("INSERT INTO usage_ledger_child VALUES ('missing')")
        with self.assertRaises(LedgerError):
            archive._seal(self.frozen, self.key, self.key_id)

    def test_live_wal_snapshot_sidecars_are_not_ignored(self):
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.frozen) + suffix)
            sidecar.write_bytes(b'synthetic')
            with self.assertRaises(LedgerError):
                archive._seal(self.frozen, self.key, self.key_id)
            sidecar.unlink()

    def test_existing_archive_is_never_overwritten(self):
        path = archive._publish(self.root, self.blob)
        with self.assertRaises(FileExistsError):
            archive._publish(self.root, b'other bytes')
        self.assertEqual(path.read_bytes(), self.blob)
        self.assertEqual(list(self.root.glob('.partial-*')), [])

    def test_failure_before_publication_leaves_no_final_or_partial(self):
        with patch.object(archive.os, 'link', side_effect=OSError('synthetic failed publication')):
            with self.assertRaises(OSError):
                archive._publish(self.root, self.blob)
        self.assertFalse((self.root / 'archive.gml').exists())
        self.assertEqual(list(self.root.glob('.partial-*')), [])

    def test_existing_partial_is_never_treated_as_committed_archive(self):
        (self.root / '.partial-interrupted').write_bytes(self.blob[:20])
        with self.assertRaises(FileNotFoundError):
            archive._read_archive(self.root / 'archive.gml')
        path = archive._publish(self.root, self.blob)
        self.assertEqual(archive._read_archive(path), self.blob)
        self.assertTrue((self.root / '.partial-interrupted').exists())

    def test_sync_failure_after_publication_does_not_claim_success_or_overwrite(self):
        sync = os.fsync
        calls = []
        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('synthetic directory fsync failure')
            return sync(fd)
        with patch.object(archive.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):
                archive._publish(self.root, self.blob)
        self.assertEqual((self.root / 'archive.gml').read_bytes(), self.blob)
        with self.assertRaises(FileExistsError):
            archive._publish(self.root, self.blob)

    def test_symlinks_and_public_permissions_rejected(self):
        link = self.root / 'link'
        link.symlink_to(self.frozen)
        with self.assertRaises(LedgerError):
            archive._read_archive(link)
        self.frozen.chmod(0o644)
        with self.assertRaises(LedgerError):
            archive._seal(self.frozen, self.key, self.key_id)
        self.root.chmod(0o755)
        with self.assertRaises(LedgerError):
            archive._publish(self.root, self.blob)

    def test_invalid_keys_ids_and_size_bounds_rejected(self):
        for key in (b'a', b'a' * 16, 'a' * 32):
            with self.assertRaises(LedgerError):
                archive._seal(self.frozen, key, self.key_id)
        for key_id in ('', '../bad', 'x' * 65):
            with self.assertRaises(LedgerError):
                archive._seal(self.frozen, self.key, key_id)
        with patch.object(archive, 'MAX_ARCHIVE', 20):
            self.denied(self.blob)
        with patch.object(archive, 'MAX_DB', 1):
            with self.assertRaises(LedgerError):
                archive._seal(self.frozen, self.key, self.key_id)

    def test_no_live_database_initialization_during_restore(self):
        with patch.object(archive, 'Ledger', side_effect=AssertionError('read-only restore inspection')):
            with self.recover() as restored:
                self.assertEqual(restored.read_bytes(), self.frozen.read_bytes())

    def test_recovery_cleanup_on_consumer_failure(self):
        with self.assertRaises(RuntimeError):
            with self.recover() as restored:
                raise RuntimeError('synthetic consumer failure')
        self.assertFalse(restored.exists())

    def test_stale_snapshot_cannot_prove_latest_deletions(self):
        self.ledger.purge_session(BASE['session_id'], retire=True, mode='api')
        with self.recover() as restored:
            with closing(sqlite3.connect(restored)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_ledger').fetchone()[0], 1)
        self.assertEqual(self.ledger.records('api'), [])
        self.assertFalse(self.ledger.append({**BASE, 'request_id': 'late'}))

    def test_restored_sequence_keeps_next_append_monotonic(self):
        with self.recover() as restored:
            with closing(sqlite3.connect(restored)) as conn:
                previous = conn.execute('SELECT MAX(id) FROM usage_ledger_sequence').fetchone()[0]
            recovered = Ledger(restored)
            recovered.append({**BASE, 'request_id': 'synthetic-after-restore'})
            with closing(sqlite3.connect(restored)) as conn:
                current = conn.execute('SELECT MAX(id) FROM usage_ledger_sequence').fetchone()[0]
            self.assertGreater(current, previous)

    def test_cli_rejects_source_restore_target_and_key_arguments(self):
        for flag in ('--source', '--target', '--restore', '--key'):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                archive.main(['--mock', flag, '/synthetic-only'])

    def test_cli_failure_is_redacted_and_never_changes_hold(self):
        out = io.StringIO()
        with patch.object(archive, 'mock_rehearsal', side_effect=OSError('synthetic-private-value')):
            with redirect_stdout(out):
                self.assertEqual(archive.main(['--mock']), 1)
        self.assertNotIn('synthetic-private-value', out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['production'], 'HOLD')

    def test_output_contains_no_raw_rows_keys_or_paths(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(archive.main(['--mock']), 0)
        text = out.getvalue()
        for value in ('synthetic-session', 'synthetic-key', 'cursor_key', 'snapshot_sha256', str(self.root)):
            self.assertNotIn(value, text)


if __name__ == '__main__':
    unittest.main()
