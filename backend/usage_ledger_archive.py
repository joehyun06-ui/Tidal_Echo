"""GM-00C encrypted disk rehearsal, synthetic data only; no runtime integration.

Only --mock is exposed. Private helpers are a test prototype, not an approved
production backup/restore API. No credentials, environment, network or scheduler.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import tempfile
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .usage_ledger import Ledger, LedgerError
from .usage_ledger_backup import _schema, snapshot

MAGIC = b'GM00C\x00\x01'
MAX_DB = 16 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_ARCHIVE = len(MAGIC) + 12 + 16 + 4 + MAX_MANIFEST + MAX_DB
ERROR = 'ledger_archive_unavailable'


def _deny():
    raise LedgerError(ERROR)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _deny()
        result[key] = value
    return result


def _aad(key, key_id):
    if (type(key) is not bytes or len(key) != 32 or not isinstance(key_id, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key_id)):
        _deny()
    return MAGIC + key_id.encode('ascii')


def _private(path, directory=False):
    """Trusted, single-owner temporary workspace only; not race-hardening."""
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        _deny()
    return info


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _inspect(path):
    """Inspect an owned CLOSED, standalone snapshot, never a live WAL source."""
    if not 0 < _private(path).st_size <= MAX_DB:
        _deny()
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _deny()
    data = path.read_bytes()
    if len(data) > MAX_DB or data[:16] != b'SQLite format 3\x00':
        _deny()
    deadline = time.monotonic() + 10
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=.2)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        _schema(conn)
        if conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            _deny()
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            _deny()
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            _deny()
        schema = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        counts = {}
        for kind, name, _, _ in schema:
            if kind != 'table':
                continue
            if not re.fullmatch(r'usage_ledger[a-z_]*|sqlite_sequence', name):
                _deny()
            counts[name] = conn.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
        user_version = conn.execute('PRAGMA user_version').fetchone()[0]
        return {'snapshot_sha256': hashlib.sha256(data).hexdigest(), 'snapshot_size': len(data),
                'schema_sha256': hashlib.sha256(_json([user_version, schema])).hexdigest(),
                'user_version': user_version,
                'table_counts': counts}, data


def _seal(path, key, key_id):
    aad = _aad(key, key_id)
    facts, data = _inspect(path)
    manifest = {'archive_version': 1, 'scope': 'dedicated_ledger_only',
                'created_at': datetime.now(timezone.utc).isoformat(),
                'sqlite_version': sqlite3.sqlite_version, **facts}
    metadata = _json(manifest)
    if len(metadata) > MAX_MANIFEST:
        _deny()
    nonce = os.urandom(12)
    plaintext = struct.pack('>I', len(metadata)) + metadata + data
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, aad), facts['schema_sha256']


def _decode(blob, key, key_id, expected_schema):
    aad = _aad(key, key_id)
    if (type(blob) is not bytes or not len(MAGIC) + 12 + 16 + 4 < len(blob) <= MAX_ARCHIVE
            or not blob.startswith(MAGIC) or not isinstance(expected_schema, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_schema)):
        _deny()
    nonce = blob[len(MAGIC):len(MAGIC) + 12]
    plaintext = AESGCM(key).decrypt(nonce, blob[len(MAGIC) + 12:], aad)
    size = struct.unpack('>I', plaintext[:4])[0]
    if not 0 < size <= MAX_MANIFEST or size + 4 >= len(plaintext):
        _deny()
    manifest = json.loads(plaintext[4:4 + size], object_pairs_hook=_unique)
    fields = {'archive_version', 'scope', 'created_at', 'sqlite_version', 'snapshot_sha256',
              'snapshot_size', 'schema_sha256', 'user_version', 'table_counts'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or type(manifest['archive_version']) is not int or manifest['archive_version'] != 1
            or manifest['scope'] != 'dedicated_ledger_only'
            or manifest['schema_sha256'] != expected_schema
            or type(manifest['snapshot_size']) is not int
            or type(manifest['user_version']) is not int
            or not isinstance(manifest['created_at'], str) or len(manifest['created_at']) > 64
            or not isinstance(manifest['sqlite_version'], str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', manifest['sqlite_version'])
            or not isinstance(manifest['table_counts'], dict)
            or any(type(v) is not int or v < 0 for v in manifest['table_counts'].values())):
        _deny()
    if datetime.fromisoformat(manifest['created_at']).utcoffset() is None:
        _deny()
    data = plaintext[4 + size:]
    if (not 0 < len(data) <= MAX_DB or len(data) != manifest['snapshot_size']
            or hashlib.sha256(data).hexdigest() != manifest['snapshot_sha256']):
        _deny()
    return manifest, data


def _publish(directory, blob):
    """Local disk prototype: fsync + exclusive publication, no overwrites."""
    _private(directory, directory=True)
    if type(blob) is not bytes or not 0 < len(blob) <= MAX_ARCHIVE:
        _deny()
    final = directory / 'archive.gml'
    fd, name = tempfile.mkstemp(prefix='.partial-', dir=directory)
    partial = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-filesystem hard link makes a complete file visible exclusively.
        os.link(partial, final)
        partial.unlink()
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return final
    finally:
        partial.unlink(missing_ok=True)


def _read_archive(path):
    if not 0 < _private(path).st_size <= MAX_ARCHIVE:
        _deny()
    with path.open('rb') as stream:
        data = stream.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        _deny()
    return data


@contextmanager
def _recover(blob, key, key_id, expected_schema, *, authorized=False):
    """No target argument. A disposable disk directory is the ONLY destination."""
    try:
        if authorized is not True:
            _deny()
        manifest, data = _decode(blob, key, key_id, expected_schema)
        with tempfile.TemporaryDirectory(prefix='gm00c-isolated-') as name:
            path = Path(name) / 'restored.sqlite'
            _write_new(path, data)
            facts, _ = _inspect(path)
            if any(manifest[k] != value for k, value in facts.items()):
                _deny()
            # Do not construct Ledger here: its constructor mutates schema/WAL.
            yield path
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, InvalidTag, struct.error):
        raise LedgerError(ERROR) from None


def mock_rehearsal():
    """Reopen encrypted bytes from disk, authenticate, then validate isolated DB."""
    from .usage_ledger_storage import maintain, page
    with tempfile.TemporaryDirectory(prefix='gm00c-synthetic-') as name:
        root = Path(name)
        source = root / 'source.sqlite'
        ledger = Ledger(source)
        base = {'mode': 'api', 'provider_id': 'synthetic-api', 'request_id': 'one',
                'turn_id': 'one', 'session_id': 'synthetic-session',
                'at': '2026-10-23T02:00:00Z', 'model': 'synthetic-model',
                'dialect': 'openai_responses', 'raw_usage': {'input_tokens': 10, 'output_tokens': 2},
                'usage_complete': True, 'status': 'completed'}
        with closing(ledger.connect()) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT COUNT(*) FROM usage_ledger').fetchone()
            for n in range(3):
                ledger.append({**base, 'request_id': str(n), 'turn_id': str(n)})
            ledger.save_settings('api', 0, {'budgets': []})
            ledger.purge_session('synthetic-retired', retire=True, mode='api')
            maintain(ledger, 'api', raw_before='2026-10-24T00:00:00Z')
            cursor = page(ledger, 'api', base['session_id'], limit=1)['next_cursor']
            wal = Path(str(source) + '-wal')
            before = (source.read_bytes(), wal.read_bytes())
            frozen = root / 'snapshot.sqlite'
            snapshot(source, frozen)
            checks = {'uncheckpointed_wal_present': bool(before[1]),
                      'source_main_and_wal_unchanged': before == (source.read_bytes(), wal.read_bytes())}
        key = AESGCM.generate_key(bit_length=256)
        blob, schema = _seal(frozen, key, 'synthetic-key-v1')
        vault = root / 'synthetic-local-vault'
        vault.mkdir(mode=0o700)
        archive = _publish(vault, blob)
        del blob
        with _recover(_read_archive(archive), key, 'synthetic-key-v1', schema, authorized=True) as restored:
            checks['snapshot_bytes_exact'] = restored.read_bytes() == frozen.read_bytes()
            checks['private_file_permissions'] = restored.stat().st_mode & 0o777 == 0o600
            # Explicitly mutate only the synthetic disposable copy for behavior checks.
            recovered = Ledger(restored)
            checks['cursor_preserved'] = bool(page(recovered, 'api', base['session_id'], limit=1, cursor=cursor)['records'])
            checks['settings_preserved'] = recovered.settings('api')['revision'] == 1
            checks['retirement_preserved'] = not recovered.append({**base, 'session_id': 'synthetic-retired'})
            checks['raw_expiry_preserved'] = all(r['raw_usage'] is None for r in recovered.records('api'))
        checks['isolated_copy_removed'] = not restored.exists()
        if not all(checks.values()):
            _deny()
        return {'ok': True, 'basis': 'synthetic_local_disk_only', 'checks': checks,
                'cipher': 'AES-256-GCM', 'authenticated_manifest': True,
                'offsite_verified': False, 'power_loss_verified': False,
                'production_backup_created': False, 'production_restore_verified': False,
                'enables_production': False, 'production': 'HOLD'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mock', action='store_true', required=True)
    parser.parse_args(argv)
    try:
        print(json.dumps(mock_rehearsal(), sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error, InvalidTag):
        print(json.dumps({'ok': False, 'error': ERROR, 'production': 'HOLD', 'enables_production': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
