"""GM-02F default-off session metadata snapshots and CAS bulk metadata merge.

No topology changes, raw bulk replacement, HTTP install, creation or dispatch.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import re

from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_commands import authorize_config_command
from .session_lifecycle_coordination import digest, encode
from .web_session_provider_authority import WebSessionProviderAuthority

READ = 'session.metadata.read.v1'
SAVE = 'session.metadata.save.v1'
MAX_ROWS = 512
FIELDS = frozenset({'id','title','since_id','created_at','pinned','provider'})
IMMUTABLE = ('id','since_id','created_at','provider')


def fail(category='coordination_sessions_invalid'):
    raise LifecycleError(category)


@dataclass(frozen=True, repr=False)
class SessionMetadataRow:
    id: str
    title: str
    since_id: int
    created_at: str
    pinned: bool
    provider: str


@dataclass(frozen=True, repr=False)
class SessionListSnapshot:
    generation: int
    snapshot_sha: str
    rows: tuple[SessionMetadataRow, ...]
    stored_active: str | None

    def as_rows(self):
        return [asdict(row) for row in self.rows]


class CoordinatedSessionListCommands:
    production_supported = False
    all_writers_integrated = False
    http_integrated = False

    def __init__(self, coordinator, *, session_lock, authorize, historical_provider, enabled=False):
        self.coordinator, self.session_lock = coordinator, session_lock
        self._authorize, self._historical = authorize, historical_provider
        self.enabled = enabled is True

    def assert_session_lock(self, lock):
        if lock is not self.session_lock: fail('coordination_lock_mismatch')

    def _admit(self, access, action):
        if not self.enabled: fail('lifecycle_disabled')
        authorize_config_command(self._authorize, access, action, None)
        if not callable(self._historical): fail('coordination_history_unavailable')

    def _history(self, sid):
        try: result = self._historical(sid)
        except Exception: raise LifecycleError('coordination_history_unavailable') from None
        if result is not None and (type(result) is not str or result not in {'api','codex'}):
            fail('coordination_history_unavailable')
        return result

    def _guard(self, access, action, rows=()):
        authorize_config_command(self._authorize, access, action, None)
        for row in rows:
            authorize_config_command(self._authorize, access, action, row['id'])
        for row in rows:
            historical = self._history(row['id'])
            if historical is not None and historical != row['provider']:
                fail('coordination_session_changed')

    def _projection(self, legacy):
        config = legacy.load_config()
        raw = config.get('sessions')
        if type(raw) is not list or len(raw) > MAX_ROWS: fail()
        authority = WebSessionProviderAuthority(legacy, historical_provider=self._history)
        rows = authority.session_rows()  # validates duplicate ids, history and tombstones
        present = 'active_session' in config
        active = config.get('active_session')
        ids = {row['id'] for row in rows}
        if present and (type(active) is not str or (active != '' and active not in ids)): fail()
        value = {'contract':'guiting.session.metadata.v1','rows':rows,
                 'active_present':present,'active_value':active}
        return config, rows, active, digest(encode(value))

    def snapshot_sessions(self, *, access):
        self._admit(access, READ)
        observed = []
        def observe(legacy, generation):
            _, rows, active, sha = self._projection(legacy)
            observed[:] = rows
            self._guard(access, READ, rows)
            return SessionListSnapshot(generation, sha, tuple(SessionMetadataRow(**row) for row in rows), active)
        with self.session_lock:
            return self.coordinator.observe_config(observe, guard=lambda:self._guard(access, READ, observed))

    @staticmethod
    def _rows(rows):
        if type(rows) is not list or len(rows) > MAX_ROWS: fail()
        rows = json.loads(encode({'rows':rows}))['rows']
        for row in rows:
            if (type(row) is not dict or set(row) != FIELDS or not _session_id(row.get('id'))
                    or type(row['title']) is not str or type(row['created_at']) is not str
                    or type(row['since_id']) is not int or row['since_id'] < 0
                    or type(row['pinned']) is not bool or type(row['provider']) is not str
                    or row['provider'] not in {'api','codex'}): fail()
        if len({row['id'] for row in rows}) != len(rows): fail()
        return rows

    def save_sessions(self, request_id, rows, active=None, *, access, expected_generation,
                      expected_snapshot_sha, recover=False):
        self._admit(access, SAVE)
        if (type(expected_generation) is not int or not 0 <= expected_generation < 2**63 - 1
                or type(expected_snapshot_sha) is not str or not re.fullmatch('[0-9a-f]{64}', expected_snapshot_sha)
                or type(recover) is not bool): fail('coordination_invalid')
        rows = self._rows(rows)
        if active is not None and (not _session_id(active) or active not in {row['id'] for row in rows}): fail()
        self._guard(access, SAVE, rows)
        parameters = {'contract':'guiting.session-list.command.v1','principal':access.principal_id,
                      'action':SAVE,'body':{'rows':rows,'active':active,
                      'expected_generation':expected_generation,'expected_snapshot_sha':expected_snapshot_sha}}
        guard = lambda:self._guard(access, SAVE, rows)
        def mutate(legacy):
            config, current, _, sha = self._projection(legacy)
            if sha != expected_snapshot_sha: fail('coordination_cas_conflict')
            if (len(rows) != len(current)
                    or any(any(new[key] != old[key] for key in IMMUTABLE) for new, old in zip(rows,current))):
                fail('coordination_session_topology_unsupported')
            authority = WebSessionProviderAuthority(legacy, historical_provider=self._history)
            next_rows = deepcopy(config['sessions'])
            for raw, old, requested in zip(next_rows,current,rows):
                normalized = authority.normalize_row(requested)
                for key in ('title','pinned'):
                    if normalized[key] != old[key]: raw[key] = normalized[key]
            config['sessions'] = next_rows
            if active is not None: config['active_session'] = active
            legacy.save_config(config)
        with self.session_lock:
            options = {'guard':guard,'expected_generation':expected_generation}
            if recover:
                return self.coordinator.recover(request_id, expected_parameters=parameters, **options)
            return self.coordinator.mutate(request_id, parameters, mutate, **options)
