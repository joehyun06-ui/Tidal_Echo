"""GM-02H default-off explicit API branch copy and config publication.

Canonical copy and pending intent share a transaction. JSON publication remains
recoverable, not cross-store atomic. No native, version alternatives or ingress.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import uuid

from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_api_creation import CoordinatedApiSessionCommands
from .session_lifecycle_coordination import digest, encode
from .session_lifecycle_session_lists import MAX_ROWS
from .session_lifecycle_storage import _unique
from .web_session_context import defaults, validate as validate_context
from .web_session_provider_authority import WebSessionProviderAuthority

READ = 'session.api.fork.read.v1'
FORK = 'session.api.fork.v1'
MAX_HISTORY_BYTES = 8 * 1024 * 1024
FORK_DDL = 'CREATE TABLE web_session_forks (target TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)'
CONTEXT_DDL = ('CREATE TABLE web_session_context (root_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, '
               'config TEXT NOT NULL, updated_at TEXT NOT NULL)')


def fail(category='coordination_fork_invalid'):
    raise LifecycleError(category)


def private_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: fail('coordination_fork_source_invalid'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LifecycleError('coordination_fork_source_invalid') from None


def history_sha(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_HISTORY_BYTES: fail('coordination_fork_history_too_large')
        return hashlib.sha256(raw).hexdigest()
    except LifecycleError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LifecycleError('coordination_fork_source_invalid') from None


@dataclass(frozen=True, repr=False)
class ApiForkSnapshot:
    expected_generation: int
    expected_snapshot_sha: str
    expected_source_sha: str
    expected_copy_sha: str


class CoordinatedApiForkCommands(CoordinatedApiSessionCommands):
    cross_store_atomic = False

    def __init__(self, coordinator, *, canonical_path, **kwargs):
        super().__init__(coordinator, **kwargs)
        self.canonical_path = Path(canonical_path)

    def assert_database_path(self, path):
        if not self.enabled: fail('lifecycle_disabled')
        try:
            requested = Path(path)
            if (not requested.is_absolute() or '..' in requested.parts
                    or requested != self.canonical_path): fail('coordination_resource_mismatch')
        except (TypeError, ValueError):
            raise LifecycleError('coordination_resource_mismatch') from None

    def _database(self, conn):
        self.assert_database_path(self.canonical_path)
        actual = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
        if actual != str(self.canonical_path): fail('coordination_resource_mismatch')

    @staticmethod
    def _table(conn, name, columns):
        row = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None: return False
        actual = {r[1]: (r[2].upper(), r[5]) for r in conn.execute('PRAGMA table_info('+name+')')}
        if (row[0] != 'table' or actual != columns or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE tbl_name=? AND type='trigger'", (name,)).fetchone()):
            fail('coordination_fork_schema_invalid')
        return True

    def _tables(self, conn):
        return (self._table(conn, 'web_session_forks', {'target':('TEXT',1),'fingerprint':('TEXT',0)}),
                self._table(conn, 'web_session_context', {'root_id':('TEXT',1),'revision':('INTEGER',0),
                    'config':('TEXT',0),'updated_at':('TEXT',0)}))

    def _unversioned(self, conn, sid):
        scopes = {'web_message_versions': {'version_id','root_id','parent_id'},
                  'web_version_selections': {'root_id','version_id'}}
        for table, columns in scopes.items():
            row = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
            if row is None: continue
            actual = {r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            if row[0] != 'table' or not columns <= actual: fail('coordination_fork_schema_invalid')
            where = ' OR '.join(column+'=?' for column in sorted(columns))
            if conn.execute('SELECT 1 FROM '+table+' WHERE '+where+' LIMIT 1', (sid,)*len(columns)).fetchone():
                fail('coordination_fork_versions_unsupported')

    def _request(self, sid, body, title, created_at):
        if not _session_id(sid) or type(body) is not dict or set(body) != {'request_id','message_id','mode'}: fail()
        if type(body['request_id']) is not str: fail()
        try: rid = uuid.UUID(body['request_id']).hex
        except (ValueError, AttributeError): raise LifecycleError('coordination_fork_invalid') from None
        if type(body['message_id']) is not int or body['message_id'] <= 0: fail()
        if type(body['mode']) is not str or body['mode'] != 'branch': fail('coordination_fork_mode_unsupported')
        target = 'api-fork-'+rid
        if sid == target: fail()
        row = self._prepare({'id':target,'title':title,'since_id':0,
            'created_at':created_at,'pinned':False,'provider':'api'})
        return {'request_id':str(uuid.UUID(rid)), 'message_id':body['message_id'], 'mode':'branch'}, row

    def _config(self, legacy, sid, row, expected_sha=None):
        config, rows, _, sha = self._projection(legacy)
        if expected_sha is not None and sha != expected_sha: fail('coordination_cas_conflict')
        source = next((r for r in rows if r['id'] == sid), None)
        if source is None: fail('coordination_fork_source_unavailable')
        if source['provider'] != 'api': fail('coordination_native_fork_unsupported')
        authority = WebSessionProviderAuthority(legacy, historical_provider=self._history)
        if (any(r['id'] == row['id'] for r in rows) or authority.tombstone_for_session(row['id'])
                or self._history(row['id']) is not None): fail('coordination_session_conflict')
        if len(rows) >= MAX_ROWS: fail('coordination_creation_capacity')
        return config, sha

    def _source(self, conn, sid, body, row):
        self._database(conn)
        _, context_present = self._tables(conn)
        self._unversioned(conn, sid)
        rows = [dict(r) for r in conn.execute(
            "SELECT id,ts,direction,kind,text,meta FROM messages WHERE id<=? "
            "AND json_extract(meta,'$.api_session')=? AND kind IN ('user','voice','reply') ORDER BY id LIMIT 5001",
            (body['message_id'],sid))]
        if len(rows) > 5000: fail('coordination_fork_history_too_large')
        if not rows or rows[-1]['id'] != body['message_id']: fail('coordination_fork_message_unavailable')
        copied = []
        for value in rows:
            if (any(type(value[k]) is not str for k in ('ts','direction','kind','text','meta'))
                    or value['direction'] not in {'in','out'}): fail('coordination_fork_source_invalid')
            original = private_json(value['meta'])
            if type(original) is not dict or original.get('api_session') != sid: fail('coordination_fork_source_invalid')
            meta = {'api_session':row['id'],'source':'web_fork',
                    'branch_origin':{'session_id':sid,'message_id':value['id']}}
            if isinstance(original.get('attachments'),list): meta['attachments'] = original['attachments']
            copied.append({**{k:value[k] for k in ('ts','direction','kind','text')},'meta':meta})
        context = None
        if context_present:
            value = conn.execute('SELECT * FROM web_session_context WHERE root_id=?', (sid,)).fetchone()
            context = dict(value) if value else None
        try:
            config = validate_context(private_json(context['config'])) if context else defaults()
            if context and (type(context['revision']) is not int or context['revision'] < 0
                            or type(context['updated_at']) is not str): fail('coordination_fork_source_invalid')
        except ValueError: raise LifecycleError('coordination_fork_source_invalid') from None
        target_context = {'root_id':row['id'],'revision':1,'config':config,'updated_at':row['created_at']}
        source_sha = history_sha({'rows':rows,'context':context,'context_table_present':context_present})
        copy_sha = history_sha({'rows':copied,'context':target_context})
        return source_sha, copy_sha, copied, target_context

    def snapshot_fork(self, sid, body, *, title, created_at, access):
        self._admit(access, READ)
        body, row = self._request(sid, body, title, created_at)
        scopes = [{'id':sid,'provider':'api'},row]
        guard = lambda:self._guard(access,READ,scopes)
        guard()
        row = WebSessionProviderAuthority(None,historical_provider=self._history).normalize_row(row)
        def observe(legacy, generation, conn):
            _, metadata_sha = self._config(legacy,sid,row)
            self.coordinator._fresh_session(conn,row['id'])
            source_sha,copy_sha,_,_ = self._source(conn,sid,body,row)
            return ApiForkSnapshot(generation,metadata_sha,source_sha,copy_sha)
        with self.session_lock:
            return self.coordinator.observe_canonical(observe,guard=guard)

    def fork_conversation(self, sid, body, *, title, created_at, access, expected_generation,
                          expected_snapshot_sha, expected_source_sha, expected_copy_sha, recover=False):
        self._admit(access, FORK)
        body, row = self._request(sid,body,title,created_at)
        if (type(expected_generation) is not int or not 0 <= expected_generation < 2**63-1
                or type(recover) is not bool or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v)
                    for v in (expected_snapshot_sha,expected_source_sha,expected_copy_sha))): fail()
        scopes = [{'id':sid,'provider':'api'},row]
        guard = lambda:self._guard(access,FORK,scopes)
        guard()
        row = WebSessionProviderAuthority(None,historical_provider=self._history).normalize_row(row)
        parameters = {'contract':'guiting.api-fork.command.v1','action':FORK,'principal':access.principal_id,
            'source':sid,'body':body,'row':row,'activate':False,'expected_generation':expected_generation,
            'expected_snapshot_sha':expected_snapshot_sha,'expected_source_sha':expected_source_sha,
            'expected_copy_sha':expected_copy_sha}
        fingerprint = digest(encode(parameters))
        def mutate(legacy):
            config,_ = self._config(legacy,sid,row,expected_snapshot_sha)
            config['sessions'].append(dict(row))
            legacy.save_config(config)
        def effect(conn):
            source_sha,copy_sha,copied,context = self._source(conn,sid,body,row)
            if source_sha != expected_source_sha or copy_sha != expected_copy_sha:
                fail('coordination_fork_source_changed')
            if not self._tables(conn)[0]: conn.execute(FORK_DDL)
            if not self._tables(conn)[1]: conn.execute(CONTEXT_DDL)
            conn.execute('INSERT INTO web_session_forks VALUES (?,?)',(row['id'],fingerprint))
            conn.execute('INSERT INTO web_session_context VALUES (?,?,?,?)',
                (row['id'],1,json.dumps(context['config'],ensure_ascii=False),row['created_at']))
            for value in copied:
                conn.execute('INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,?,?,?,?)',
                    tuple(value[k] for k in ('ts','direction','kind','text'))+(json.dumps(value['meta'],ensure_ascii=False),))
        def validate_pending(conn):
            self._database(conn)
            if self._tables(conn) != (True,True): fail('coordination_fork_copy_changed')
            receipt = conn.execute('SELECT fingerprint FROM web_session_forks WHERE target=?',(row['id'],)).fetchone()
            if not receipt or receipt[0] != fingerprint: fail('coordination_fork_copy_changed')
            copied = [dict(r) for r in conn.execute(
                "SELECT ts,direction,kind,text,meta FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id LIMIT 5001",(row['id'],))]
            for value in copied: value['meta'] = private_json(value['meta'])
            value = conn.execute('SELECT * FROM web_session_context WHERE root_id=?',(row['id'],)).fetchone()
            if value is None: fail('coordination_fork_copy_changed')
            context = dict(value);context['config'] = private_json(context['config'])
            if len(copied)>5000 or history_sha({'rows':copied,'context':context}) != expected_copy_sha:
                fail('coordination_fork_copy_changed')
        with self.session_lock:
            options = {'guard':guard,'expected_generation':expected_generation,'pending_validator':validate_pending}
            if recover:
                return self.coordinator.recover(row['id'],expected_parameters=parameters,**options)
            return self.coordinator.mutate(row['id'],parameters,mutate,require_fresh_session=row['id'],
                canonical_effect=effect,**options)
