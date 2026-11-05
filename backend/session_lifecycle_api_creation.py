"""GM-02G default-off explicit creation/publication of fresh empty API rows.

Caller must retain a stable server-allocated ID/timestamp/request packet. Never
used for lifecycle reentry, native Codex creation, history forks or dispatch.
"""
import datetime as dt
from pathlib import Path
import re

from .session_lifecycle import LifecycleError
from .session_lifecycle_session_lists import CoordinatedSessionListCommands, MAX_ROWS
from .web_session_provider_authority import WebSessionProviderAuthority

CREATE = 'session.api.create.v1'
PUBLISH = 'session.api.publish.v1'


def fail(category='coordination_creation_invalid'):
    raise LifecycleError(category)


class CoordinatedApiSessionCommands(CoordinatedSessionListCommands):
    native_integrated = False

    def assert_config_path(self, path):
        if not self.enabled: fail('lifecycle_disabled')
        try:
            requested = Path(path)
            if (not requested.is_absolute() or '..' in requested.parts
                    or requested != self.coordinator.config): fail('coordination_resource_mismatch')
        except (TypeError, ValueError, AttributeError):
            raise LifecycleError('coordination_resource_mismatch') from None

    def _prepare(self, row):
        try: result = self._rows([row])[0]
        except LifecycleError: raise LifecycleError('coordination_creation_invalid') from None
        if result['provider'] != 'api': fail('coordination_native_creation_unsupported')
        if result['since_id'] != 0 or result['pinned'] is not False:
            fail('coordination_creation_scope_unsupported')
        stamp = result['created_at']
        try:
            if (not stamp or len(stamp)>80 or stamp != stamp.strip()
                    or not stamp.endswith(('Z','+00:00')) or any(ord(c)<32 or ord(c)==127 for c in stamp)):
                fail()
            parsed = dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if parsed.utcoffset() != dt.timedelta(0): fail()
        except (ValueError, OverflowError):
            raise LifecycleError('coordination_creation_invalid') from None
        return result

    def _publish(self, request_id, row, activate, *, action, access,
                 expected_generation, expected_snapshot_sha, recover):
        self._admit(access, action)
        if (type(activate) is not bool or type(recover) is not bool
                or type(expected_generation) is not int or not 0 <= expected_generation < 2**63-1
                or type(expected_snapshot_sha) is not str or not re.fullmatch('[0-9a-f]{64}',expected_snapshot_sha)):
            fail()
        row = self._prepare(row)
        self._guard(access,action,[row])
        row = WebSessionProviderAuthority(None,historical_provider=self._history).normalize_row(row)
        parameters = {'contract':'guiting.api-session.command.v1','principal':access.principal_id,
                      'action':action,'target':row['id'],'body':{'row':row,'activate':activate,
                      'expected_generation':expected_generation,'expected_snapshot_sha':expected_snapshot_sha}}
        guard = lambda:self._guard(access,action,[row])
        def mutate(legacy):
            config,current,_,sha = self._projection(legacy)
            if sha != expected_snapshot_sha: fail('coordination_cas_conflict')
            authority = WebSessionProviderAuthority(legacy,historical_provider=self._history)
            if (any(item['id']==row['id'] for item in current)
                    or authority.tombstone_for_session(row['id']) is not None or self._history(row['id']) is not None):
                fail('coordination_session_conflict')
            if len(current)>=MAX_ROWS: fail('coordination_creation_capacity')
            config['sessions'].append(dict(row))
            if activate: config['active_session']=row['id']
            legacy.save_config(config)
        with self.session_lock:
            options={'guard':guard,'expected_generation':expected_generation,'require_fresh_session':row['id']}
            if recover:
                return self.coordinator.recover(request_id,expected_parameters=parameters,**options)
            return self.coordinator.mutate(request_id,parameters,mutate,**options)

    def create_api_session(self, *, title='New chat', since_id=0, activate=True, session_id=None,
                           created_at=None, request_id, access, expected_generation,
                           expected_snapshot_sha, recover=False):
        row={'id':session_id,'title':title,'since_id':since_id,'created_at':created_at,'pinned':False,'provider':'api'}
        return self._publish(request_id,row,activate,action=CREATE,access=access,
            expected_generation=expected_generation,expected_snapshot_sha=expected_snapshot_sha,recover=recover)

    def publish_row(self, row, *, activate, request_id, access, expected_generation,
                    expected_snapshot_sha, recover=False):
        return self._publish(request_id,row,activate,action=PUBLISH,access=access,
            expected_generation=expected_generation,expected_snapshot_sha=expected_snapshot_sha,recover=recover)
