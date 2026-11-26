"""Explicit, file-backed synthetic lifecycle fixture. No runtime integration."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class Denied(ValueError):
    """The requested transition has insufficient or conflicting evidence."""


def require(condition, category):
    if not condition:
        raise Denied(category)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Target:
    conversation: str
    owner: str
    window: str
    epoch: int


@dataclass(frozen=True)
class Snapshot:
    pending: tuple[str, ...]
    active_state: str
    recent_anchors: tuple[str, ...]
    summary_candidates: tuple[str, ...]


@dataclass(frozen=True)
class SourceRef:
    cursor: int
    event: str
    revision: int
    sha256: str


SCHEMA = """
CREATE TABLE genesis(singleton INTEGER PRIMARY KEY CHECK(singleton=1), conversation TEXT NOT NULL,
 owner TEXT NOT NULL, first_window TEXT NOT NULL);
CREATE TABLE authority(singleton INTEGER PRIMARY KEY CHECK(singleton=1), revoked INTEGER NOT NULL);
CREATE TABLE turn(singleton INTEGER PRIMARY KEY CHECK(singleton=1), job TEXT NOT NULL,
 reply TEXT NOT NULL, tool TEXT NOT NULL, streaming INTEGER NOT NULL, unresolved INTEGER NOT NULL);
CREATE TABLE continuation(window TEXT PRIMARY KEY);
CREATE TABLE handoff(id TEXT PRIMARY KEY, old TEXT NOT NULL UNIQUE, epoch INTEGER NOT NULL UNIQUE,
 candidate TEXT NOT NULL UNIQUE, input_hash TEXT NOT NULL, CHECK(old<>candidate));
CREATE TABLE operation(id TEXT REFERENCES handoff(id), kind TEXT NOT NULL,
 request_hash TEXT NOT NULL, phase TEXT NOT NULL, crossed INTEGER NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(id,kind,request_hash), CHECK(kind IN ('close','prepare','switch')),
 CHECK((phase='FAILED_PRE_BOUNDARY' AND crossed=0) OR
       (phase IN ('UNKNOWN_POST_BOUNDARY','COMMITTED') AND crossed=1)));
CREATE UNIQUE INDEX one_dispatched_operation ON operation(id,kind) WHERE crossed=1;
CREATE TABLE closeout(id TEXT PRIMARY KEY REFERENCES handoff(id), body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE package(id TEXT PRIMARY KEY REFERENCES closeout(id), body TEXT NOT NULL, version TEXT NOT NULL);
CREATE TABLE final(epoch INTEGER PRIMARY KEY CHECK(epoch>0), id TEXT NOT NULL UNIQUE REFERENCES package(id),
 window TEXT NOT NULL UNIQUE, version TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE source(cursor INTEGER PRIMARY KEY CHECK(cursor>0), event TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL CHECK(revision>0), sha256 TEXT NOT NULL, allowed_owner TEXT, revoked INTEGER NOT NULL);
CREATE TABLE accepted(cursor INTEGER PRIMARY KEY REFERENCES source(cursor), event TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE VIEW head AS SELECT window,epoch FROM
 (SELECT first_window AS window,0 AS epoch FROM genesis UNION ALL SELECT window,epoch FROM final)
 ORDER BY epoch DESC LIMIT 1;
CREATE TRIGGER final_guard BEFORE INSERT ON final BEGIN
 SELECT CASE WHEN safe_commit_writer()<>1 OR NOT EXISTS
 (SELECT 1 FROM head, handoff h JOIN package p ON p.id=h.id WHERE h.id=NEW.id
 AND head.window=h.old AND head.epoch=h.epoch AND NEW.epoch=head.epoch+1
 AND NEW.window=h.candidate AND NEW.version=p.version) THEN RAISE(ABORT,'invalid switch') END;
END;
"""
IMMUTABLE = ('genesis', 'handoff', 'closeout', 'final', 'accepted')


def completion(job, reply, window):
    provider = job.get('provider')
    if provider not in ('api', 'codex') or (provider == 'codex' and job.get('native') is not True):
        return 'unsupported'
    keys = ['window', 'provider', 'message', 'reply', 'generation']
    if provider == 'codex':
        keys.extend(('client', 'callback'))
    elif job.get('upstream_unknown') is not False or job.get('generation') != 'api-gen-' + str(job.get('message')):
        return 'unknown'
    if (job.get('window') != window or job.get('status') != 'completed'
            or reply.get('canonical') is not True
            or any(not job.get(k) or job[k] != reply.get(k) for k in keys)):
        return 'unknown'
    return 'completed'


def noop(_):
    pass


class Lifecycle:
    COMMIT_CHECKS = ('owner', 'not_revoked', 'old_window', 'epoch', 'state_version',
                     'closeout_committed', 'sealed', 'completion', 'sends', 'pending_tool',
                     'candidate', 'not_active', 'package_verified', 'source_valid', 'cursor',
                     'single_winner', 'final_insert', 'final_evidence')

    def __init__(self, path):
        self.path = Path(path).resolve()
        with self._tx() as db:
            require(db.execute('PRAGMA quick_check').fetchall()[0][0] == 'ok', 'corrupt')
            require(db.execute('PRAGMA user_version').fetchone()[0] == 1, 'schema')
            require(db.execute('SELECT count(*) FROM head').fetchone()[0] == 1, 'missing head')

            require(db.execute('PRAGMA foreign_key_check').fetchone() is None, 'broken references')
            expected_epoch = 0
            previous = db.execute('SELECT first_window FROM genesis').fetchone()[0]
            for row in db.execute("""SELECT f.*,h.old,h.candidate,p.version AS package_version
                                     FROM final f JOIN handoff h USING(id) JOIN package p USING(id)
                                     ORDER BY f.epoch"""):
                expected_epoch += 1
                require(row['epoch'] == expected_epoch and row['old'] == previous
                        and row['candidate'] == row['window'] and row['version'] == row['package_version'],
                        'broken switch chain')
                previous = row['window']
            for kind, table in [('close', 'closeout'), ('prepare', 'package'), ('switch', 'final')]:
                committed = {r[0] for r in db.execute(
                    "SELECT id FROM operation WHERE kind=? AND phase='COMMITTED'", (kind,))}
                recorded = {r[0] for r in db.execute(f'SELECT id FROM {table}')}
                require(committed == recorded, 'receipt disagreement')
                if table != 'final':
                    for key in recorded:
                        self._body(db, table, key)

    @classmethod
    def create_fixture(cls, path, target):
        require(target.epoch == 0 and all((target.conversation, target.owner, target.window))
                and target.conversation != target.window, 'genesis')
        path = Path(path)
        path.touch(exist_ok=False)
        db = sqlite3.connect(path)
        try:
            db.executescript(SCHEMA)
            for table in IMMUTABLE:
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"""CREATE TRIGGER lock_{table}_{action} BEFORE {action} ON {table}
                                BEGIN SELECT RAISE(ABORT,'immutable'); END""")
            db.execute('INSERT INTO genesis VALUES(1,?,?,?)', (target.conversation, target.owner, target.window))
            db.execute('INSERT INTO authority VALUES(1,0)')
            db.execute('PRAGMA user_version=1')
            db.commit()
        finally:
            db.close()
        return cls(path)

    @contextmanager
    def _tx(self, *, write=False, switch=False):
        mode = 'rw' if write else 'ro'
        db = sqlite3.connect(f'{self.path.as_uri()}?mode={mode}', uri=True, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.create_function('safe_commit_writer', 0, lambda: int(switch))
            db.execute('PRAGMA foreign_keys=ON')
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        finally:
            db.close()  # uncommitted writes roll back

    @staticmethod
    def _owner(db, target):
        genesis = db.execute('SELECT * FROM genesis').fetchone()
        require(genesis and (genesis['conversation'], genesis['owner']) ==
                (target.conversation, target.owner), 'owner')
        require(db.execute('SELECT revoked FROM authority').fetchone()[0] == 0, 'revoked')

    @staticmethod
    def _current(db, target):
        head = db.execute('SELECT * FROM head').fetchone()
        require(head['window'] == target.window, 'stale window')
        require(head['epoch'] == target.epoch, 'stale epoch')

    def head(self, target):
        with self._tx() as db:
            self._owner(db, target)
            return dict(db.execute('SELECT * FROM head').fetchone())

    def send_allowed(self, target):
        with self._tx() as db:
            self._owner(db, target)
            row = db.execute("""SELECT 1 FROM head WHERE window=? AND epoch=?
                AND NOT EXISTS(SELECT 1 FROM handoff WHERE old=head.window)""",
                             (target.window, target.epoch)).fetchone()
            turn = db.execute('SELECT * FROM turn').fetchone()
            if turn and json.loads(turn['job']).get('window') == target.window:
                if (turn['streaming'] or turn['unresolved'] or turn['tool'] != 'settled'
                        or completion(json.loads(turn['job']), json.loads(turn['reply']), target.window)
                        != 'completed'):
                    return False
            return bool(row)

    def request_continuation(self, target):
        with self._tx(write=True) as db:
            self._owner(db, target)
            self._current(db, target)
            db.execute('INSERT OR IGNORE INTO continuation VALUES(?)', (target.window,))

    def continuation_pending(self, target):
        with self._tx() as db:
            self._owner(db, target)
            self._current(db, target)
            return bool(db.execute('SELECT 1 FROM continuation WHERE window=? UNION ALL '
                                   'SELECT 1 FROM handoff WHERE old=?', (target.window, target.window)).fetchone())

    # The following writes belong exclusively to the synthetic authority fixture.
    def fixture_turn(self, job, reply, *, tool='settled', streaming=False, unresolved=False):
        with self._tx(write=True) as db:
            db.execute('INSERT OR REPLACE INTO turn VALUES(1,?,?,?,?,?)',
                       (encode(job), encode(reply), tool, streaming, unresolved))

    def fixture_owner_revoked(self):
        with self._tx(write=True) as db:
            db.execute('UPDATE authority SET revoked=1')

    def fixture_source(self, ref, *, owner, revoked=False):
        require(len(ref.sha256) == 64 and all(ch in '0123456789abcdef' for ch in ref.sha256), 'source digest')
        with self._tx(write=True) as db:
            existing = db.execute('SELECT event,revision,sha256 FROM source WHERE cursor=?',
                                  (ref.cursor,)).fetchone()
            if existing:
                require(existing['event'] == ref.event and ref.revision >= existing['revision']
                        and (ref.revision != existing['revision'] or existing['sha256'] == ref.sha256),
                        'source history conflict')
            db.execute("""INSERT INTO source VALUES(?,?,?,?,?,?) ON CONFLICT(cursor) DO UPDATE SET
                       revision=excluded.revision,sha256=excluded.sha256,allowed_owner=excluded.allowed_owner,
                       revoked=excluded.revoked WHERE source.event=excluded.event""",
                       (ref.cursor, ref.event, ref.revision, ref.sha256, owner, revoked))

    @staticmethod
    def _turn(db, target):
        row = db.execute('SELECT * FROM turn').fetchone()
        require(row is not None, 'missing turn')
        job, reply = json.loads(row['job']), json.loads(row['reply'])
        state = completion(job, reply, target.window)
        require(state == 'completed', state)
        require(not row['streaming'] and row['tool'] == 'settled' and not row['unresolved'], 'busy turn')
        return dict(completion_state=state, turn_digest=digest([job, reply]), tool_boundary=row['tool'])

    @staticmethod
    def _refs(db, target):
        refs = []
        for row in db.execute('SELECT * FROM accepted ORDER BY cursor'):
            ref = dict(row)
            require(ref['cursor'] == len(refs) + 1, 'cursor gap')
            Lifecycle._source(db, target, ref)
            refs.append(ref)
        return refs

    @staticmethod
    def _source(db, target, ref):
        row = db.execute('SELECT * FROM source WHERE cursor=?', (ref['cursor'],)).fetchone()
        require(row and row['allowed_owner'] == target.owner and not row['revoked']
                and all(row[k] == v for k, v in ref.items()), 'invalid source')

    @staticmethod
    def _handoff(db, target, key):
        row = db.execute('SELECT * FROM handoff WHERE id=?', (key,)).fetchone()
        require(row and (row['old'], row['epoch']) == (target.window, target.epoch), 'request binding')
        return row

    @staticmethod
    def _body(db, table, key):
        column = 'hash' if table == 'closeout' else 'version'
        row = db.execute(f'SELECT body,{column} FROM {table} WHERE id=?', (key,)).fetchone()
        require(row is not None, 'missing ' + table)
        value = json.loads(row['body'])
        require(digest(value) == row[column], 'corrupt ' + table)
        return value

    def _current_or_committed(self, db, target, identity):
        phase = db.execute('SELECT phase FROM operation WHERE id=? AND kind=? AND request_hash=?',
                           identity).fetchone()[0]
        if phase != 'COMMITTED':
            self._current(db, target)

    def _operation(self, target, key, kind, request, reserve, perform, result, fault):
        """Durable admission, then dispatch intent, then one conditional SQLite mutation."""
        request_hash = digest([asdict(target), key, kind, request])
        identity = (key, kind, request_hash)

        def lookup(db):
            self._owner(db, target)
            row = db.execute('SELECT * FROM operation WHERE id=? AND kind=? AND request_hash=?',
                             identity).fetchone()
            if row is None:
                prior = db.execute('SELECT max(crossed) FROM operation WHERE id=? AND kind=?',
                                   (key, kind)).fetchone()[0]
                require(prior is None or (kind == 'switch' and prior == 0), 'payload conflict')
            elif row['phase'] != 'COMMITTED':
                require(not row['crossed'], 'reconcile only')
            return row

        with self._tx() as db:
            existing = lookup(db)
            if existing and existing['phase'] == 'COMMITTED':
                return result(db)
        with self._tx(write=True) as db:
            existing = lookup(db)
            if existing and existing['phase'] == 'COMMITTED':
                return result(db)
            self._current(db, target)
            reserve(db)
            if not existing:
                db.execute("INSERT INTO operation VALUES(?,?,?,'FAILED_PRE_BOUNDARY',0,?)",
                           (key, kind, request_hash, 'reserved; no dispatch'))
        fault('pre_boundary')
        with self._tx(write=True) as db:
            self._owner(db, target)
            self._current_or_committed(db, target, identity)
            armed = db.execute("""UPDATE operation SET phase='UNKNOWN_POST_BOUNDARY',crossed=1,
                evidence='dispatch intent' WHERE id=? AND kind=? AND request_hash=? AND crossed=0""", identity)
            if not armed.rowcount:
                row = db.execute('SELECT phase FROM operation WHERE id=? AND kind=? AND request_hash=?', identity).fetchone()
                require(row[0] == 'COMMITTED', 'reconcile only')
                return result(db)
        fault('dispatch')
        # Only a confirmed rollback of this local mutation can disarm its intent.
        # Commit/transport exceptions outside the body deliberately leave it armed.
        rolled_back = False
        try:
            with self._tx(write=True, switch=kind == 'switch') as db:
                try:
                    self._owner(db, target)
                    self._current(db, target)
                    perform(db)
                    db.execute("""UPDATE operation SET phase='COMMITTED',evidence='local commit receipt'
                               WHERE id=? AND kind=? AND request_hash=?""", identity)
                    if kind == 'switch':
                        fault('final_evidence')
                    value = result(db)
                except Exception:
                    db.rollback()
                    rolled_back = True
                    raise
        except Exception:
            if rolled_back:
                with self._tx(write=True) as db:
                    db.execute("""UPDATE operation SET phase='FAILED_PRE_BOUNDARY',crossed=0,
                               evidence='local rollback confirmed; no effect' WHERE id=? AND kind=? AND request_hash=?""", identity)
            raise
        fault('response_loss')
        return value

    def closeout(self, target, key, snapshot, *, fault=noop):
        payload = asdict(snapshot)

        def reserve(db):
            require(isinstance(key, str) and key, 'closeout id')
            self._turn(db, target)
            current = db.execute('SELECT id FROM handoff WHERE old=?', (target.window,)).fetchone()
            require(not current or current['id'] == key, 'busy window')
            if not current:
                db.execute('INSERT INTO handoff VALUES(?,?,?,?,?)',
                           (key, target.window, target.epoch, 'win-' + uuid4().hex, digest(payload)))

        def perform(db):
            h = self._handoff(db, target, key)
            refs = self._refs(db, target)
            body = dict(**payload, **self._turn(db, target), conversation_id=target.conversation,
                        window_id=target.window, context_epoch=target.epoch, closeout_id=key,
                        candidate=h['candidate'], source_cursor=len(refs), created_at=now(), schema_version=1)
            db.execute('INSERT INTO closeout VALUES(?,?,?)', (key, encode(body), digest(body)))

        return self._operation(target, key, 'close', payload, reserve, perform,
                               lambda db: self._body(db, 'closeout', key), fault)

    @staticmethod
    def _recovery_body(close, refs):
        body = dict(close)
        body.update(previous_window=close['window_id'], previous_epoch=close['context_epoch'],
                    window_id=close['candidate'], context_epoch=close['context_epoch'] + 1,
                    source_refs=refs, source_cursor=len(refs),
                    context_budget={'usage': 'unknown', 'automatic_threshold': False})
        return body

    def _package(self, db, target, key):
        body = self._body(db, 'package', key)
        h = self._handoff(db, target, key)
        require(body['candidate'] == h['candidate'], 'candidate')
        refs = self._refs(db, target)
        close = self._body(db, 'closeout', key)
        require(body == self._recovery_body(close, refs), 'package drift')
        return dict(**body, version=digest(body))

    def reentry(self, target, key, *, fault=noop):
        def reserve(db):
            self._handoff(db, target, key)
            self._body(db, 'closeout', key)

        def perform(db):
            close = self._body(db, 'closeout', key)
            refs = self._refs(db, target)
            body = self._recovery_body(close, refs)
            db.execute('INSERT INTO package VALUES(?,?,?)', (key, encode(body), digest(body)))

        def result(db):
            self._current(db, target)
            return self._package(db, target, key)

        return self._operation(target, key, 'prepare', {}, reserve, perform, result, fault)

    def reentry_delta(self, target, key, page):
        """Apply a contiguous fixture page. Its entries are references, never message bodies."""
        with self._tx(write=True) as db:
            self._owner(db, target)
            self._current(db, target)
            self._handoff(db, target, key)
            package = self._package(db, target, key)
            row = db.execute("SELECT max(crossed) FROM operation WHERE id=? AND kind='switch'", (key,)).fetchone()
            require(not row or not row[0], 'reconcile only')
            refs = package['source_refs']
            previous = None
            for item in page:
                ref = asdict(item)
                self._source(db, target, ref)
                require(previous is None or item.cursor == previous + 1, 'out of order')
                previous = item.cursor
                if item.cursor <= len(refs):
                    require(item.cursor > 0 and refs[item.cursor - 1] == ref, 'duplicate conflict')
                    continue
                require(item.cursor == len(refs) + 1, 'cursor gap')
                db.execute('INSERT INTO accepted VALUES(?,?,?,?)',
                           (item.cursor, item.event, item.revision, item.sha256))
                refs.append(ref)
            package.pop('version')
            package['source_cursor'] = len(refs)
            db.execute('UPDATE package SET body=?, version=? WHERE id=?',
                       (encode(package), digest(package), key))
            return self._package(db, target, key)

    def safe_commit_point(self, target, key, version, *, fault=noop):
        def reserve(db):
            self._handoff(db, target, key)
            require(self._package(db, target, key)['version'] == version, 'stale package')

        def perform(db):
            def check(name, condition):
                require(condition, name)
                fault(name)
            # _operation already checked authority and head in this same transaction.
            for name in ('owner', 'not_revoked', 'old_window', 'epoch'):
                fault(name)
            h = self._handoff(db, target, key)
            package = self._body(db, 'package', key)
            check('state_version', digest(package) == version)
            close = self._body(db, 'closeout', key)
            phases = dict(db.execute('SELECT kind,phase FROM operation WHERE id=?', (key,)))
            check('closeout_committed', phases.get('close') == 'COMMITTED')
            check('sealed', h['old'] == close['window_id'])
            turn = self._turn(db, target)
            check('completion', close['turn_digest'] == turn['turn_digest'])
            check('sends', not db.execute('SELECT unresolved FROM turn').fetchone()[0])
            check('pending_tool', package['pending'] == close['pending'] and
                  close['tool_boundary'] == turn['tool_boundary'])
            check('candidate', h['candidate'] == close['candidate'] == package['candidate'])
            first = db.execute('SELECT first_window FROM genesis').fetchone()[0]
            check('not_active', h['candidate'] not in (target.window, first) and not db.execute(
                'SELECT 1 FROM final WHERE window=?', (h['candidate'],)).fetchone())
            check('package_verified', phases.get('prepare') == 'COMMITTED' and
                  package == self._recovery_body(close, package['source_refs']))
            refs = self._refs(db, target)
            check('source_valid', refs == package['source_refs'])
            upper = db.execute('SELECT coalesce(max(cursor),0) FROM source').fetchone()[0]
            check('cursor', close['source_cursor'] <= len(refs) == package['source_cursor'] == upper)
            check('single_winner', not db.execute('SELECT 1 FROM final WHERE id=?', (key,)).fetchone())
            db.execute('INSERT INTO final VALUES(?,?,?,?,?)',
                       (target.epoch + 1, key, h['candidate'], version, now()))
            fault('final_insert')

        def result(db):
            return dict(db.execute('SELECT * FROM final WHERE id=?', (key,)).fetchone())

        return self._operation(target, key, 'switch', version, reserve, perform, result, fault)

    def reconcile(self, target, key, kind):
        """Read only operation metadata and final receipt. Never redispatch or restore context."""
        with self._tx() as db:
            self._owner(db, target)
            h = self._handoff(db, target, key)
            row = db.execute('SELECT * FROM operation WHERE id=? AND kind=? ORDER BY crossed DESC,rowid DESC LIMIT 1', (key, kind)).fetchone()
            require(row, 'no operation evidence')
            final = db.execute('SELECT * FROM final WHERE id=?', (key,)).fetchone()
            if row['phase'] == 'COMMITTED':
                if kind == 'switch':
                    require(final and final['window'] == h['candidate'] and final['epoch'] == h['epoch'] + 1,
                            'inconsistent final evidence')
                else:
                    self._body(db, 'closeout' if kind == 'close' else 'package', key)
            return dict(**dict(row), candidate=h['candidate'], old_window=h['old'],
                        last_confirmed_phase=row['evidence'], mutation_boundary_crossed=bool(row['crossed']),
                        reconcile_only=row['phase'] == 'UNKNOWN_POST_BOUNDARY',
                        final_receipt=dict(final) if final else None)
