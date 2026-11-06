"""GM-02C offline candidate: cooperative config writers and lifecycle fences.

Explicit bootstrap only. All participating RMW operations must enter mutate();
wrapping save_config alone is insufficient. Trusted local POSIX filesystem and
parents are required. Advisory locks do not constrain legacy/manual writers.
Lock order: existing session lock -> this gate -> canonical SQLite transaction.
Never enter a session lock from inside this gate. No runtime installation here.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time

from .deployment_config import LOOP_CONFIG_MAX_BYTES, atomic_write_text
from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_storage import _unique, _source_schema

FORMAT = 'guiting.coordination.v1'
DDL = (
    '''CREATE TABLE session_coordination_meta (
        id INTEGER PRIMARY KEY CHECK(id=1), format TEXT NOT NULL,
        binding TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
        config_sha TEXT NOT NULL)''',
    '''CREATE TABLE session_coordination_ops (
        request_id TEXT PRIMARY KEY NOT NULL, input_sha TEXT NOT NULL,
        base_generation INTEGER NOT NULL, old_sha TEXT NOT NULL, new_sha TEXT NOT NULL,
        stage TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','committed')))''',
    "CREATE UNIQUE INDEX session_coordination_pending ON session_coordination_ops(status) WHERE status='pending'",
)


def fail(category='coordination_unavailable'):
    raise LifecycleError(category)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    try:
        def check(item):
            if type(item) is dict:
                if any(type(key) is not str for key in item): fail('coordination_invalid')
                for child in item.values(): check(child)
            elif type(item) is list:
                for child in item: check(child)
            elif type(item) not in (str, int, float, bool, type(None)):
                fail('coordination_invalid')
        check(value)
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
        if not 0 < len(raw) <= LOOP_CONFIG_MAX_BYTES: fail('coordination_invalid')
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LifecycleError('coordination_invalid') from None


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= LOOP_CONFIG_MAX_BYTES):
            fail('coordination_private_file_required')
        raw = handle.read(LOOP_CONFIG_MAX_BYTES + 1)
        end = os.fstat(handle.fileno())
    current = Path(path).lstat()
    token = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if token(info) != token(end) or token(end) != token(current) or len(raw) != info.st_size:
        fail('coordination_external_change')
    return raw


def parse(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: fail('coordination_invalid'))
        if not isinstance(value, dict): fail('coordination_invalid')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise LifecycleError('coordination_invalid') from None


class _Gate:
    def __init__(self):
        self.lock, self.local = threading.RLock(), threading.local()


_gates, _registry_lock = {}, threading.Lock()


@dataclass(frozen=True)
class WriteReceipt:
    generation: int
    replayed: bool


@dataclass(frozen=True)
class CommittedOperationProof:
    request_id: str
    input_sha: str
    base_generation: int


class _BufferedLegacy:
    """Only the JSON portion of the legacy interface; callback is trusted/pure."""
    def __init__(self, config): self.config = deepcopy(config)
    def load_config(self): return deepcopy(self.config)
    def save_config(self, config): self.config = deepcopy(config)


class _ReadonlyLegacy:
    def __init__(self, config): self._config = deepcopy(config)
    def load_config(self): return deepcopy(self._config)


class SharedSessionCoordinator:
    production_supported = False
    all_writers_integrated = False

    def __init__(self, connect_canonical, config_path, gate_dir, *, validate, enabled=False, timeout=5, canonical_path=None):
        self._connect, self.config, self.directory = connect_canonical, Path(config_path), Path(gate_dir)
        self.validate, self.enabled, self.timeout = validate, enabled is True, timeout
        self.canonical_path = canonical_path
        # Constructors have no IO; defaults never open a file or database.

    @contextmanager
    def _locked(self, *, write=False):
        if not self.enabled: fail('lifecycle_disabled')
        if os.name != 'posix': fail('coordination_platform_unsupported')
        import fcntl
        if (not self.config.is_absolute() or not self.directory.is_absolute()
                or '..' in self.config.parts or '..' in self.directory.parts
                or self.config.resolve() != self.config or self.directory.resolve() != self.directory
                or not isinstance(self.timeout, (int, float)) or not 0 < self.timeout <= 60):
            fail('coordination_invalid')
        key = (os.getpid(), str(self.directory))
        with _registry_lock: gate = _gates.setdefault(key, _Gate())
        deadline = time.monotonic() + self.timeout
        if not gate.lock.acquire(timeout=self.timeout): fail('coordination_busy')
        fd = None
        try:
            depth = getattr(gate.local, 'depth', 0)
            if depth and write: fail('coordination_nested_write')
            if not depth:
                info = self.directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    fail('coordination_private_directory_required')
                lock_path = self.directory / 'writer.lock'
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    fail('coordination_private_file_required')
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline: fail('coordination_busy')
                        time.sleep(min(.01, max(0, deadline - time.monotonic())))
                current = lock_path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    fail('coordination_external_change')
            gate.local.depth = depth + 1
            try: yield
            finally: gate.local.depth = depth
        except (OSError, sqlite3.Error):
            raise LifecycleError('coordination_unavailable') from None
        finally:
            if fd is not None: os.close(fd)
            gate.lock.release()

    @contextmanager
    def _db(self, *, write=True):
        with closing(self._connect()) as conn:
            if conn.in_transaction or conn.isolation_level is not None: fail('coordination_connection_invalid')
            try:
                if not write: conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                _source_schema(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _binding(self, conn):
        rows = conn.execute('PRAGMA database_list').fetchall()
        path = next((r[2] for r in rows if r[1] == 'main'), '')
        if not path or Path(path).resolve() != Path(path): fail('coordination_connection_invalid')
        if self.canonical_path is not None:
            try:
                expected = Path(self.canonical_path)
                if (not expected.is_absolute() or '..' in expected.parts or expected.resolve() != expected
                        or str(expected) != path): fail('coordination_resource_mismatch')
            except (TypeError, ValueError, OSError, RuntimeError):
                raise LifecycleError('coordination_resource_mismatch') from None
        return digest(encode([path, str(self.config), str(self.directory)]))

    def _meta(self, conn):
        norm = lambda s: re.sub(r'\s+', ' ', s).strip()
        expected = {norm(sql) for sql in DDL}
        rows = conn.execute("SELECT type,sql FROM sqlite_master WHERE name GLOB 'session_coordination_*' "
                            "OR tbl_name IN ('session_coordination_meta','session_coordination_ops')").fetchall()
        # Autoindexes have NULL SQL; extra indexes/triggers/views are not trusted.
        rows = [r for r in rows if not (r[0] == 'index' and r[1] is None)]
        if (len(rows) != 3 or any(not isinstance(r[1], str) for r in rows)
                or {norm(r[1]) for r in rows} != expected): fail('coordination_schema_invalid')
        meta = conn.execute('SELECT format,binding,generation,config_sha FROM session_coordination_meta WHERE id=1').fetchone()
        if (not meta or meta[0] != FORMAT or meta[1] != self._binding(conn)
                or type(meta[2]) is not int or meta[2] < 0 or not re.fullmatch('[0-9a-f]{64}', meta[3])):
            fail('coordination_schema_invalid')
        return meta[2], meta[3]

    def bootstrap(self):
        """Explicit synthetic migration; never called by runtime startup."""
        with self._locked(write=True):
            raw = private_read(self.config)
            self.validate(deepcopy(parse(raw)))
            with self._db() as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'session_coordination_*'").fetchone():
                    self._ready(conn, raw)
                    return False
                for sql in DDL: conn.execute(sql)
                conn.execute('INSERT INTO session_coordination_meta VALUES (1,?,?,0,?)',
                             (FORMAT, self._binding(conn), digest(raw)))
                self._meta(conn)
                return True

    def _ready(self, conn, raw):
        generation, sha = self._meta(conn)
        if conn.execute("SELECT 1 FROM session_coordination_ops WHERE status='pending'").fetchone():
            fail('coordination_recovery_required')
        if digest(raw) != sha: fail('coordination_external_change')
        return generation, sha

    @contextmanager
    def fence(self):
        with self._locked():
            with self._db(write=False) as conn: self._ready(conn, private_read(self.config))
            yield
            with self._db(write=False) as conn: self._ready(conn, private_read(self.config))

    def observe_config(self, observer, *, guard=None):
        """Trusted read-only projection under the cooperative gate, no intent.

        The callback receives a private copy and generation, not a connection or
        writable legacy API. Its return value must be scoped/redacted by caller.
        Factories must still target the existing canonical database. This is not
        a transaction with uncooperative writers, runtime or external authority.
        """
        return self._observe(observer, guard=guard, canonical=False)

    def observe_canonical(self, observer, *, guard=None):
        """Trusted projection of config and query-only canonical DB, no intent."""
        return self._observe(observer, guard=guard, canonical=True)

    def _observe(self, observer, *, guard, canonical):
        with self._locked():
            if guard is not None: guard()
            raw = private_read(self.config)
            with self._db(write=False) as conn:
                generation, sha = self._ready(conn, raw)
                legacy = _ReadonlyLegacy(parse(raw))
                result = observer(legacy, generation, conn) if canonical else observer(legacy, generation)
                if guard is not None: guard()
                if private_read(self.config) != raw or self._ready(conn, raw) != (generation, sha):
                    fail('coordination_external_change')
                return result

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)

    def mutate_canonical(self, request_id, parameters, effect, *, guard=None, expected_generation=None):
        """Trusted SQL-only effect, generation and committed receipt in one txn.

        Config stays byte-identical. The effect receives a read-only legacy
        config and must not commit, use external IO or dispatch. A private empty
        stage preserves the existing journal/cleanup format; it is not a JSON
        publication intent. Crash before commit rolls everything back; after
        commit replay/recover returns the historical receipt without rerunning
        SQL. This does not atomically fence external config/auth/runtime writers.
        """
        if (type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',request_id)
                or type(parameters) is not dict or not callable(effect)): fail('coordination_invalid')
        input_sha = digest(encode(parameters))
        with self._locked(write=True):
            if guard is not None: guard()
            raw = private_read(self.config)
            with self._db() as conn:
                generation, sha = self._ready(conn,raw)
                old = conn.execute('SELECT input_sha,base_generation,stage,status FROM session_coordination_ops WHERE request_id=?',
                                   (request_id,)).fetchone()
                if old:
                    if old[0] != input_sha: fail('coordination_request_conflict')
                    if old[3] != 'committed': fail('coordination_recovery_required')
                    stage = old[2]
                    if type(stage) is not str or not re.fullmatch(r'config-[A-Za-z0-9_-]+\.stage',stage):
                        fail('coordination_journal_invalid')
                    receipt = WriteReceipt(old[1]+1,True)
                else:
                    self._preconditions(conn,generation,sha,expected_generation,None)
                    effect(conn,_ReadonlyLegacy(parse(raw)))
                    if guard is not None: guard()
                    if private_read(self.config) != raw: fail('coordination_external_change')
                    fd, path = tempfile.mkstemp(prefix='config-',suffix='.stage',dir=self.directory)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                    self._sync_directory()
                    stage = Path(path).name
                    conn.execute("INSERT INTO session_coordination_ops VALUES (?,?,?,?,?,?,'committed')",
                                 (request_id,input_sha,generation,sha,sha,stage))
                    conn.execute('UPDATE session_coordination_meta SET generation=? WHERE id=1',(generation+1,))
                    receipt = WriteReceipt(generation+1,False)
            (self.directory/stage).unlink(missing_ok=True)
            self._sync_directory()
            return receipt

    def _preconditions(self, conn, generation, sha, expected_generation, require_operation):
        if expected_generation is not None:
            if type(expected_generation) is not int or not 0 <= expected_generation < 2**63 - 1:
                fail('coordination_invalid')
            if generation != expected_generation: fail('coordination_cas_conflict')
        if require_operation is not None:
            proof = require_operation
            if (type(proof) is not CommittedOperationProof or type(proof.request_id) is not str
                    or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', proof.request_id)
                    or type(proof.input_sha) is not str or not re.fullmatch('[0-9a-f]{64}', proof.input_sha)
                    or type(proof.base_generation) is not int or not 0 <= proof.base_generation < 2**63 - 1):
                fail('coordination_invalid')
            row = conn.execute('SELECT input_sha,base_generation,new_sha,status FROM session_coordination_ops WHERE request_id=?',
                               (proof.request_id,)).fetchone()
            if (not row or tuple(row) != (proof.input_sha, proof.base_generation, sha, 'committed')
                    or generation != proof.base_generation + 1):
                fail('coordination_operation_conflict')

    def _fresh_session(self, conn, sid):
        if sid is None: return
        if not _session_id(sid): fail('coordination_invalid')
        invalid = conn.execute("SELECT 1 FROM messages WHERE CASE WHEN json_valid(meta) THEN "
            "json_type(meta)!='object' OR COALESCE(json_type(meta,'$.api_session') NOT IN ('text','null'),0) "
            "ELSE 1 END LIMIT 1").fetchone()
        if invalid: fail('coordination_namespace_invalid')
        if conn.execute("SELECT 1 FROM messages WHERE json_extract(meta,'$.api_session')=? LIMIT 1",(sid,)).fetchone():
            fail('coordination_session_namespace_conflict')
        scopes = {
            'web_message_versions':('version_id','root_id','parent_id'),
            'web_version_selections':('root_id','version_id'),
            'web_session_forks':('target',),
            'web_session_context':('root_id',),
            'api_web_generations':('session_id',),
            'api_web_closed_sessions':('session_id',),
            'session_lifecycle_records':('conversation_id',),
        }
        for table, columns in scopes.items():
            kind = conn.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
            if not kind: continue
            actual = {row[1] for row in conn.execute('PRAGMA table_info('+table+')')}
            if kind[0] != 'table' or not set(columns) <= actual: fail('coordination_namespace_invalid')
            where = ' OR '.join(column+'=?' for column in columns)  # identifiers are fixed above
            if conn.execute('SELECT 1 FROM '+table+' WHERE '+where+' LIMIT 1',(sid,)*len(columns)).fetchone():
                fail('coordination_session_namespace_conflict')

    def mutate(self, request_id, parameters, callback, *, guard=None,
               expected_generation=None, require_operation=None, require_fresh_session=None,
               canonical_effect=None, pending_validator=None):
        """Whole RMW under gate. A committed replay does not invoke callback.

        After a durable intent, exceptions mean outcome unknown: recover(id),
        then replay the same ID. Before that intent, pure computation may repeat
        after a crash. A callback must only edit its buffered config. Parameters
        must bind the complete action/target/payload; no route/auth is added here.
        Optional CAS/proof preconditions must also be bound into parameters by
        the caller. Committed replay is historical and does not reapply CAS.
        canonical_effect is trusted transaction-local SQL only: no commits,
        external IO, models or dispatch. It commits with the pending intent,
        never reruns in recovery/replay. pending_validator verifies those
        effects before an unpublished intent becomes visible. Callers bind
        both contracts and all effect digests into parameters. Not an atomic
        transaction with JSON, native state or uncooperative runtime ingress.
        """
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request_id):
            fail('coordination_invalid')
        if type(parameters) is not dict: fail('coordination_invalid')
        if canonical_effect is not None and (not callable(canonical_effect) or not callable(pending_validator)):
            fail('coordination_invalid')
        input_sha = digest(encode(parameters))
        with self._locked(write=True):
            if guard is not None: guard()
            raw = private_read(self.config)
            with self._db() as conn:
                generation, old_sha = self._ready(conn, raw)
                old = conn.execute('SELECT input_sha,base_generation FROM session_coordination_ops WHERE request_id=?', (request_id,)).fetchone()
                if old:
                    if old[0] != input_sha: fail('coordination_request_conflict')
                    return WriteReceipt(old[1] + 1, True)
                self._preconditions(conn, generation, old_sha, expected_generation, require_operation)
                self._fresh_session(conn, require_fresh_session)
            legacy = _BufferedLegacy(parse(raw))
            callback(legacy)
            self.validate(deepcopy(legacy.config))
            if guard is not None: guard()
            new = encode(legacy.config)
            if canonical_effect is not None and digest(new) == digest(raw):
                fail('coordination_effect_requires_config_change')
            # Validation/callback may call arbitrary trusted code; recheck before intent.
            if private_read(self.config) != raw: fail('coordination_external_change')
            fd, stage = tempfile.mkstemp(prefix='config-', suffix='.stage', dir=self.directory)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(new); handle.flush(); os.fsync(handle.fileno())
                self._sync_directory()
                with self._db() as conn:
                    if self._ready(conn, private_read(self.config)) != (generation, old_sha):
                        fail('coordination_external_change')
                    self._preconditions(conn, generation, old_sha, expected_generation, require_operation)
                    self._fresh_session(conn, require_fresh_session)
                    if canonical_effect is not None:
                        canonical_effect(conn)
                        if guard is not None: guard()
                    conn.execute('INSERT INTO session_coordination_ops VALUES (?,?,?,?,?,?,\'pending\')',
                                 (request_id, input_sha, generation, old_sha, digest(new), Path(stage).name))
            except BaseException:
                # A commit exception can have an uncertain outcome. Keep the stage;
                # only explicit recovery may decide whether a durable intent exists.
                raise
            return self._recover(request_id, replayed=False, guard=guard,
                                 expected_generation=expected_generation, require_operation=require_operation,
                                 require_fresh_session=None if canonical_effect is not None else require_fresh_session,
                                 pending_validator=pending_validator)

    def recover(self, request_id, *, expected_parameters=None, guard=None,
                expected_generation=None, require_operation=None, require_fresh_session=None,
                pending_validator=None):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request_id):
            fail('coordination_invalid')
        expected_sha = digest(encode(expected_parameters)) if expected_parameters is not None else None
        with self._locked(write=True):
            if guard is not None: guard()
            return self._recover(request_id, replayed=True, expected_sha=expected_sha, guard=guard,
                                 expected_generation=expected_generation, require_operation=require_operation,
                                 require_fresh_session=require_fresh_session, pending_validator=pending_validator)

    def _recover(self, request_id, *, replayed, expected_sha=None, guard=None,
                 expected_generation=None, require_operation=None, require_fresh_session=None,
                 pending_validator=None):
        with self._db() as conn:
            generation, sha = self._meta(conn)
            row = conn.execute('SELECT base_generation,old_sha,new_sha,stage,status,input_sha FROM session_coordination_ops WHERE request_id=?', (request_id,)).fetchone()
            if not row: fail('coordination_request_missing')
            base, old_sha, new_sha, stage, status, input_sha = row
            if expected_sha is not None and expected_sha != input_sha: fail('coordination_request_conflict')
            if not re.fullmatch(r'config-[A-Za-z0-9_-]+\.stage', stage):
                fail('coordination_journal_invalid')
            if status == 'committed':
                self._ready(conn, private_read(self.config))
                (self.directory / stage).unlink(missing_ok=True)
                self._sync_directory()
                return WriteReceipt(base + 1, True)
            self._preconditions(conn, generation, sha, expected_generation, require_operation)
            if (generation != base or sha != old_sha or not re.fullmatch(r'config-[A-Za-z0-9_-]+\.stage', stage)
                    or not re.fullmatch('[0-9a-f]{64}', new_sha)):
                fail('coordination_journal_invalid')
            # After publication normal API history may exist. Do not confuse
            # finishing that intent with a new claim that the ID is unused.
            if (require_fresh_session is not None and old_sha != new_sha
                    and digest(private_read(self.config)) == old_sha):
                self._fresh_session(conn, require_fresh_session)
            if (pending_validator is not None and old_sha != new_sha
                    and digest(private_read(self.config)) == old_sha):
                pending_validator(conn)
        current_sha = digest(private_read(self.config))
        if guard is not None: guard()
        if current_sha == old_sha and current_sha != new_sha:
            try: new = private_read(self.directory / stage)
            except OSError: raise LifecycleError('coordination_stage_unavailable') from None
            if digest(new) != new_sha: fail('coordination_stage_invalid')
            self.validate(deepcopy(parse(new)))
            atomic_write_text(self.config, new.decode('utf-8'))
        elif current_sha != new_sha:
            fail('coordination_external_change')
        with self._db() as conn:
            if self._meta(conn) != (base, old_sha) or digest(private_read(self.config)) != new_sha:
                fail('coordination_external_change')
            self._preconditions(conn, base, old_sha, expected_generation, require_operation)
            conn.execute('UPDATE session_coordination_meta SET generation=?,config_sha=? WHERE id=1', (base + 1, new_sha))
            conn.execute("UPDATE session_coordination_ops SET status='committed' WHERE request_id=? AND status='pending'", (request_id,))
        (self.directory / stage).unlink(missing_ok=True)
        self._sync_directory()
        return WriteReceipt(base + 1, replayed)
