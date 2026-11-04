"""GM-02B non-production storage candidate; no install hook or live factory.

SQLite state/receipts are atomic. File checks detect observed config conflicts,
but DO NOT fence uncooperative writers or establish cross-store atomicity.
"""
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .deployment_config import LOOP_CONFIG_MAX_BYTES
from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_codec import (
    MAX_BYTES, checksum, decode_record, decode_receipt, encode_record, encode_receipt, hex_value, integer,
)
from .web_session_provider_authority import WebSessionProviderAuthority, WebSessionProviderAuthorityError

DDL = {
    'session_lifecycle_meta': '''CREATE TABLE session_lifecycle_meta (
        id INTEGER PRIMARY KEY CHECK(id=1), format TEXT NOT NULL, codec_version INTEGER NOT NULL)''',
    'session_lifecycle_records': '''CREATE TABLE session_lifecycle_records (
        conversation_id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        payload TEXT NOT NULL, sha256 TEXT NOT NULL)''',
    'session_lifecycle_receipts': '''CREATE TABLE session_lifecycle_receipts (
        conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, revision_applied INTEGER NOT NULL,
        payload TEXT NOT NULL, sha256 TEXT NOT NULL,
        PRIMARY KEY(conversation_id,request_id),
        FOREIGN KEY(conversation_id) REFERENCES session_lifecycle_records(conversation_id))''',
}
FORMAT = 'guiting.lifecycle.repository.v1'


def fail(category='lifecycle_storage_unavailable'):
    raise LifecycleError(category)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail('lifecycle_config_invalid')
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class ConfigView:
    conversation_id: str
    provider: str
    sessions: tuple[tuple[str, str], ...]
    token: tuple


class JsonSessionReader:
    """Explicit readonly private JSON file; never logs or returns route secrets.

    historical_provider is supplied by the existing trusted session authority.
    Capturing its independent history atomically is still an integration gate.
    """
    def __init__(self, path, *, historical_provider):
        self._path = Path(path)
        self._historical = historical_provider

    def capture(self, cid):
        if not _session_id(cid): fail('lifecycle_invalid')
        if os.name != 'posix': fail('lifecycle_storage_platform_unsupported')
        try:
            if not self._path.is_absolute() or '..' in self._path.parts:
                fail('lifecycle_config_invalid')
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                first = os.fstat(stream.fileno())
                if (not stat.S_ISREG(first.st_mode) or first.st_uid != os.getuid()
                        or first.st_mode & 0o077 or not 0 < first.st_size <= LOOP_CONFIG_MAX_BYTES):
                    fail('lifecycle_config_invalid')
                raw = stream.read(LOOP_CONFIG_MAX_BYTES + 1)
                last = os.fstat(stream.fileno())
            current = self._path.lstat()
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if (identity(first) != identity(last) or identity(last) != identity(current)
                    or len(raw) != first.st_size):
                fail('lifecycle_authority_changed')
            config = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                                parse_constant=lambda _: fail('lifecycle_config_invalid'))
            if not isinstance(config, dict): fail('lifecycle_config_invalid')
            class FrozenLegacy:
                def load_config(self): return config
            authority = WebSessionProviderAuthority(FrozenLegacy(), historical_provider=self._historical)
            rows = authority.session_rows()
            row = next((item for item in rows if item['id'] == cid), None)
            if row is None or authority.tombstone_for_session(cid):
                fail('lifecycle_session_unavailable')
            return ConfigView(cid, row['provider'], tuple((r['id'], r['provider']) for r in rows),
                              identity(current) + (hashlib.sha256(raw).hexdigest(),))
        except LifecycleError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError, WebSessionProviderAuthorityError):
            raise LifecycleError('lifecycle_config_invalid') from None

    def is_current(self, view):
        try:
            return self.capture(view.conversation_id) == view
        except LifecycleError:
            return False


def _source_schema(conn):
    row = conn.execute("SELECT type FROM sqlite_master WHERE name='messages'").fetchone()
    cols = {r[1]: r for r in conn.execute('PRAGMA table_info(messages)')}
    if (not row or row[0] != 'table' or not {'id','ts','direction','kind','text','meta'} <= cols.keys()
            or str(cols['id'][2]).upper() != 'INTEGER' or cols['id'][5] != 1):
        fail('lifecycle_relay_schema_invalid')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        fail('lifecycle_connection_invalid')


def _owned(conn, *, allow_missing=False):
    objects = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name GLOB 'session_lifecycle_*' "
                           "OR tbl_name IN ('session_lifecycle_meta','session_lifecycle_records','session_lifecycle_receipts')").fetchall()
    if not objects and allow_missing:
        return False
    tables = {}
    normalize = lambda sql: re.sub(r'\s+', ' ', sql).strip()
    for kind, name, owner, sql in objects:
        if kind == 'index' and sql is None and owner in DDL:
            continue
        if kind != 'table' or name not in DDL or normalize(sql) != normalize(DDL[name]):
            fail('lifecycle_schema_invalid')
        tables[name] = True
    if set(tables) != set(DDL): fail('lifecycle_schema_invalid')
    rows = conn.execute('SELECT id,format,codec_version FROM session_lifecycle_meta').fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (1, FORMAT, 1): fail('lifecycle_schema_invalid')
    if conn.execute('PRAGMA foreign_key_check(session_lifecycle_receipts)').fetchone() is not None:
        fail('lifecycle_storage_corrupt')
    return True


def install_schema(conn):
    """Explicit candidate migration on caller-owned canonical connection only."""
    if conn.in_transaction: fail('lifecycle_connection_invalid')
    try:
        conn.execute('BEGIN IMMEDIATE'); _source_schema(conn)
        if _owned(conn, allow_missing=True):
            conn.commit(); return False
        for ddl in DDL.values(): conn.execute(ddl)
        conn.execute('INSERT INTO session_lifecycle_meta VALUES (1,?,1)', (FORMAT,))
        _owned(conn); conn.commit(); return True
    except BaseException as error:
        conn.rollback()
        if isinstance(error, (sqlite3.Error, OSError)):
            raise LifecycleError('lifecycle_storage_unavailable') from None
        raise


def rollback_empty_schema(conn):
    """Only empty candidate tables may be removed; never discard recorded work."""
    if conn.in_transaction: fail('lifecycle_connection_invalid')
    try:
        conn.execute('BEGIN IMMEDIATE'); _source_schema(conn); _owned(conn)
        if (conn.execute('SELECT 1 FROM session_lifecycle_records LIMIT 1').fetchone()
                or conn.execute('SELECT 1 FROM session_lifecycle_receipts LIMIT 1').fetchone()):
            fail('lifecycle_rollback_requires_empty')
        for table in reversed(DDL): conn.execute('DROP TABLE ' + table)
        conn.commit()
    except BaseException as error:
        conn.rollback()
        if isinstance(error, (sqlite3.Error, OSError)):
            raise LifecycleError('lifecycle_storage_unavailable') from None
        raise


def _check_digest(payload, digest):
    hex_value(digest)
    if not isinstance(payload, str) or checksum(payload) != digest:
        fail('lifecycle_storage_corrupt')


def _selected(conn, view):
    cid = view.conversation_id
    versions = conn.execute("SELECT type FROM sqlite_master WHERE name='web_message_versions'").fetchone()
    if versions:
        if versions[0] != 'table': fail('lifecycle_version_schema_invalid')
        edge = conn.execute('SELECT root_id FROM web_message_versions WHERE version_id=?', (cid,)).fetchone()
        if edge: fail('lifecycle_conversation_root_required')
    table = conn.execute("SELECT type FROM sqlite_master WHERE name='web_version_selections'").fetchone()
    version = cid
    if table:
        cols = {r[1]: r for r in conn.execute('PRAGMA table_info(web_version_selections)')}
        if table[0] != 'table' or set(cols) != {'root_id','version_id'} or cols['root_id'][5] != 1:
            fail('lifecycle_version_schema_invalid')
        row = conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?', (cid,)).fetchone()
        version = row[0] if row else cid
    providers = dict(view.sessions)
    if providers.get(version) != view.provider: fail('lifecycle_session_changed')
    if version != cid:
        table = conn.execute("SELECT type FROM sqlite_master WHERE name='web_message_versions'").fetchone()
        if not table or table[0] != 'table': fail('lifecycle_version_schema_invalid')
        row = conn.execute('SELECT root_id FROM web_message_versions WHERE version_id=?', (version,)).fetchone()
        if not row or row[0] != cid: fail('lifecycle_session_changed')
    return version


def _binding(conn, view, state):
    if (state.conversation_id != view.conversation_id or state.provider != view.provider
            or state.version_id != _selected(conn, view)):
        fail('lifecycle_session_changed')
    ids = set(state.message_refs) | ({state.message_cursor} if state.message_cursor else set())
    for mid in ids:
        row = conn.execute("SELECT json_extract(meta,'$.api_session'),kind FROM messages WHERE id=?", (mid,)).fetchone()
        if not row or row[0] != state.version_id or row[1] not in ('user', 'voice', 'reply'):
            fail('lifecycle_message_scope_changed')


class _Transaction:
    def __init__(self, conn, view):
        self.conn, self.view, self.active, self.saved = conn, view, True, False

    def _check(self):
        if not self.active: fail('lifecycle_transaction_closed')

    def load(self):
        self._check()
        row = self.conn.execute("SELECT CASE WHEN typeof(revision)='integer' THEN revision END,"
                "CASE WHEN typeof(payload)='text' AND length(payload)<=? THEN payload END,"
                "CASE WHEN typeof(sha256)='text' AND length(sha256)=64 THEN sha256 END "
                'FROM session_lifecycle_records WHERE conversation_id=?', (MAX_BYTES, self.view.conversation_id)).fetchone()
        if not row: return None
        _check_digest(row[1], row[2]); state = decode_record(row[1])
        if state.revision != row[0]: fail('lifecycle_storage_corrupt')
        _binding(self.conn, self.view, state)
        return state

    def receipt(self, request_id):
        self._check()
        row = self.conn.execute("SELECT CASE WHEN typeof(revision_applied)='integer' THEN revision_applied END,"
                "CASE WHEN typeof(payload)='text' AND length(payload)<=? THEN payload END,"
                "CASE WHEN typeof(sha256)='text' AND length(sha256)=64 THEN sha256 END "
                'FROM session_lifecycle_receipts WHERE conversation_id=? AND request_id=?',
                (MAX_BYTES, self.view.conversation_id, request_id)).fetchone()
        if not row: return None
        _check_digest(row[1], row[2]); receipt = decode_receipt(row[1]); state = self.load()
        if (state is None or receipt.request_id != request_id or receipt.revision_applied != row[0]
                or receipt.revision_applied > state.revision): fail('lifecycle_storage_corrupt')
        return receipt

    def save(self, expected_revision, state, receipt):
        self._check(); integer(expected_revision)
        if self.saved: fail('lifecycle_commit_conflict')
        record_json, receipt_json = encode_record(state), encode_receipt(receipt)
        if (state.revision != expected_revision + 1 or receipt.revision_applied != state.revision
                or receipt.phase_at_apply != state.phase): fail('lifecycle_storage_corrupt')
        _binding(self.conn, self.view, state)
        old = self.load()
        if (old.revision if old else 0) != expected_revision: return False
        if self.receipt(receipt.request_id) is not None: fail('lifecycle_request_conflict')
        args = (state.revision, record_json, checksum(record_json), state.conversation_id)
        if expected_revision:
            result = self.conn.execute('UPDATE session_lifecycle_records SET revision=?,payload=?,sha256=? '
                                      'WHERE conversation_id=? AND revision=?', (*args, expected_revision))
        else:
            result = self.conn.execute('INSERT OR IGNORE INTO session_lifecycle_records(revision,payload,sha256,conversation_id) '
                                       'VALUES (?,?,?,?)', args)
        if result.rowcount != 1: return False
        self.conn.execute('INSERT INTO session_lifecycle_receipts VALUES (?,?,?,?,?)',
                          (state.conversation_id, receipt.request_id, receipt.revision_applied, receipt_json, checksum(receipt_json)))
        self.saved = True
        return True


class SQLiteLifecycleRepository:
    """Default-off explicit canonical connection factory; no separate chat DB.

    Checksums detect accidental damage, not malicious rewrites or rollback.
    Config pre/post checks do NOT replace a reviewed common writer fence.
    """
    production_supported = False
    cross_store_atomic = False

    def __init__(self, connect_canonical, config_reader, *, enabled=False, coordinator=None):
        self._connect, self._reader, self.enabled = connect_canonical, config_reader, enabled is True
        self._coordinator = coordinator

    @contextmanager
    def transaction(self, conversation_id):
        if not self.enabled: fail('lifecycle_disabled')
        # Cooperative gate spans capture, SQLite commit, and both file checks.
        # No coordinator is installed or enabled implicitly.
        with self._coordinator.fence() if self._coordinator is not None else nullcontext():
            with self._transaction(conversation_id) as tx:
                yield tx

    @contextmanager
    def _transaction(self, conversation_id):
        view = self._reader.capture(conversation_id)
        tx = None
        try:
            with closing(self._connect()) as conn:
                if conn.in_transaction or conn.isolation_level is not None: fail('lifecycle_connection_invalid')
                try:
                    conn.execute('BEGIN IMMEDIATE'); _source_schema(conn); _owned(conn)
                    _selected(conn, view)
                    tx = _Transaction(conn, view)
                    yield tx
                    _owned(conn)
                    if not self._reader.is_current(view): fail('lifecycle_authority_changed')
                    conn.commit()
                    # A failed post-check means commit may already exist; same-ID
                    # reconciliation is required, never blind resend or success.
                    if not self._reader.is_current(view): fail('lifecycle_authority_changed_after_commit')
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    if tx: tx.active = False
        except (sqlite3.Error, OSError):
            raise LifecycleError('lifecycle_storage_unavailable') from None
