"""Default-off API window-attempt bookkeeping in the canonical lifecycle DB.

This journal never dispatches, authorizes a dispatch, or confirms consumption.
A recorded attempt is permanently outcome_unknown here, including after a lost
commit acknowledgement. No timeout, different request ID or changed packet may
create another attempt for the same closeout. Runtime integration is still open.
"""
from dataclasses import dataclass
import hashlib
import re
import sqlite3

from .g3_projection import _valid_id
from .g3_projection_feed import _binding
from .session_lifecycle import LifecycleError, _json, _session_id
from .session_lifecycle_storage import SQLiteLifecycleRepository, _owned, _source_schema

FORMAT = 'guiting.window-attempt.v1'
DDL = (
    '''CREATE TABLE session_window_attempt_meta (
        id INTEGER PRIMARY KEY CHECK(id=1), format TEXT NOT NULL)''',
    '''CREATE TABLE session_window_attempts (
        conversation_id TEXT NOT NULL, closeout_id TEXT NOT NULL,
        request_id TEXT NOT NULL, packet_sha256 TEXT NOT NULL,
        lifecycle_revision INTEGER NOT NULL CHECK(lifecycle_revision>0),
        access_binding TEXT NOT NULL, record_sha256 TEXT NOT NULL,
        PRIMARY KEY(conversation_id,closeout_id), UNIQUE(conversation_id,request_id),
        FOREIGN KEY(conversation_id) REFERENCES session_lifecycle_records(conversation_id))''',
)
COLUMNS = ('conversation_id', 'closeout_id', 'request_id', 'packet_sha256',
           'lifecycle_revision', 'access_binding', 'record_sha256')


def fail(category='lifecycle_attempt_invalid'):
    raise LifecycleError(category)


def _hex(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _checksum(values):
    return hashlib.sha256(_json(list(values))).hexdigest()


def _schema(conn, *, allow_missing=False):
    normalize = lambda sql: re.sub(r'\s+', ' ', sql).strip()
    rows = conn.execute("SELECT type,tbl_name,sql FROM sqlite_master "
                        "WHERE name GLOB 'session_window_attempt*' "
                        "OR tbl_name IN ('session_window_attempt_meta','session_window_attempts')").fetchall()
    if not rows and allow_missing:
        return False
    rows = [r for r in rows if not (r[0] == 'index' and r[2] is None
                                   and r[1] in ('session_window_attempt_meta', 'session_window_attempts'))]
    if (len(rows) != 2 or any(r[0] != 'table' or not isinstance(r[2], str) for r in rows)
            or {normalize(r[2]) for r in rows} != {normalize(sql) for sql in DDL}):
        fail('lifecycle_attempt_schema_invalid')
    if [tuple(r) for r in conn.execute('SELECT id,format FROM session_window_attempt_meta')] != [(1, FORMAT)]:
        fail('lifecycle_attempt_schema_invalid')
    if conn.execute('PRAGMA foreign_key_check(session_window_attempts)').fetchone() is not None:
        fail('lifecycle_attempt_corrupt')
    return True


def install_schema(conn):
    """Explicit caller-owned canonical migration; never installed by startup."""
    if conn.in_transaction or conn.isolation_level is not None:
        fail('lifecycle_connection_invalid')
    try:
        conn.execute('BEGIN IMMEDIATE')
        _source_schema(conn)
        _owned(conn)
        if _schema(conn, allow_missing=True):
            conn.commit()
            return False
        for sql in DDL:
            conn.execute(sql)
        conn.execute('INSERT INTO session_window_attempt_meta VALUES (1,?)', (FORMAT,))
        _schema(conn)
        conn.commit()
        return True
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, (sqlite3.Error, OSError)):
            raise LifecycleError('lifecycle_storage_unavailable') from None
        raise


@dataclass(frozen=True)
class AttemptObservation:
    conversation_id: str
    closeout_id: str
    request_id: str
    packet_sha256: str
    lifecycle_revision: int
    replayed: bool
    outcome: str = 'unknown'
    dispatch_authorized: bool = False
    window_confirmed: bool = False
    reply_completed: bool = False


def _row(conn, cid, request_id=None, closeout_id=None):
    # Bound text before Python decoding, even for damaged rows.
    selection = ','.join(
        "CASE WHEN typeof(" + c + ")='integer' THEN " + c + ' END' if c == 'lifecycle_revision'
        else "CASE WHEN typeof(" + c + ")='text' AND length(" + c + ')<=160 THEN ' + c + ' END'
        for c in COLUMNS)
    field, value = ('request_id', request_id) if request_id is not None else ('closeout_id', closeout_id)
    row = conn.execute('SELECT ' + selection + ' FROM session_window_attempts '
                       'WHERE conversation_id=? AND ' + field + '=?', (cid, value)).fetchone()
    if row is None:
        return None
    row = tuple(row)
    if (not _session_id(row[0]) or not _valid_id(row[1]) or not _valid_id(row[2])
            or not _hex(row[3]) or type(row[4]) is not int or not 1 <= row[4] <= 2**63-1
            or not _hex(row[5]) or not _hex(row[6]) or _checksum(row[:6]) != row[6]):
        fail('lifecycle_attempt_corrupt')
    return row


class WindowAttemptJournal:
    production_supported = False
    runtime_integrated = False

    def __init__(self, repository, *, enabled=False):
        # Construction/default-off paths perform no IO.
        self.repository, self.enabled = repository, enabled is True

    def _check(self, cid, request_id, access):
        if not self.enabled:
            fail('lifecycle_disabled')
        if (type(self.repository) is not SQLiteLifecycleRepository or not self.repository.enabled
                or not _session_id(cid) or not _valid_id(request_id)):
            fail()
        return _binding(access)

    @staticmethod
    def _bound(tx, binding):
        state = tx.load()
        if state is None:
            fail('lifecycle_phase_conflict')
        if state.access_binding != binding:
            fail('lifecycle_access_denied')
        if state.provider != 'api':
            fail('lifecycle_provider_unsupported')
        _schema(tx.conn)
        return state

    @staticmethod
    def _observation(row, binding, *, replayed):
        if row[5] != binding:
            fail('lifecycle_access_denied')
        return AttemptObservation(*row[:5], replayed)

    def record_attempt(self, cid, request_id, *, packet_sha256, expected_revision, access):
        binding = self._check(cid, request_id, access)
        if (not _hex(packet_sha256) or type(expected_revision) is not int
                or not 1 <= expected_revision <= 2**63-1):
            fail()
        with self.repository.transaction(cid) as tx:
            state = self._bound(tx, binding)
            old = _row(tx.conn, cid, request_id=request_id)
            if old is not None:
                if (old[3], old[4], old[5]) != (packet_sha256, expected_revision, binding):
                    fail('lifecycle_attempt_request_conflict')
                return self._observation(old, binding, replayed=True)
            if _row(tx.conn, cid, closeout_id=state.closeout_id) is not None:
                fail('lifecycle_attempt_closeout_conflict')
            if state.phase != 'prepared':
                fail('lifecycle_phase_conflict')
            if (state.packet_sha256, state.revision) != (packet_sha256, expected_revision):
                fail('lifecycle_attempt_state_changed')
            values = (cid, state.closeout_id, request_id, packet_sha256, expected_revision, binding)
            tx.conn.execute('INSERT INTO session_window_attempts VALUES (?,?,?,?,?,?,?)',
                            (*values, _checksum(values)))
            # The repository owns commit and its config checks. No observation
            # escapes to the caller if commit/post-check acknowledgement fails.
            result = self._observation((*values, _checksum(values)), binding, replayed=False)
        return result

    def inspect(self, cid, request_id, *, access):
        binding = self._check(cid, request_id, access)
        with self.repository.transaction(cid) as tx:
            self._bound(tx, binding)
            row = _row(tx.conn, cid, request_id=request_id)
            return self._observation(row, binding, replayed=True) if row is not None else None
