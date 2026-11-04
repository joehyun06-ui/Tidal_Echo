"""GM-02D explicit, default-off config-only command bindings; no HTTP install.

Only existing-session metadata patch and loop settings are supported. Creation,
publish/fork/delete/retire/migration are composite operations and remain blocked.
Authorization is a required trusted server callback, not a client-made grant.
"""
from dataclasses import dataclass
import json
import re

from .loop_config_mutations import LoopConfigMutationError, updated_config
from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_coordination import digest, encode
from .web_session_provider_authority import WebSessionProviderAuthority

ACTIONS = frozenset({'session.patch.v1', 'loop.update.v1'})


@dataclass(frozen=True, repr=False)
class ConfigCommandAccess:
    principal_id: str


class CoordinatedConfigCommands:
    production_supported = False
    all_writers_integrated = False
    http_integrated = False

    def __init__(self, coordinator, *, session_lock, authorize, historical_provider,
                 fallback_routes, render_mvp=True, enabled=False):
        self.coordinator, self.session_lock = coordinator, session_lock
        self._authorize, self._historical, self._routes = authorize, historical_provider, fallback_routes
        self.render_mvp, self.enabled = render_mvp, enabled is True

    def assert_session_lock(self, lock):
        if lock is not self.session_lock: raise LifecycleError('coordination_lock_mismatch')

    def _guard(self, access, action, target):
        if (type(access) is not ConfigCommandAccess or type(access.principal_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', access.principal_id)
                or not callable(self._authorize)):
            raise LifecycleError('coordination_access_denied')
        try: allowed = self._authorize(access, action, target)
        except Exception: raise LifecycleError('coordination_access_unavailable') from None
        if allowed is not True: raise LifecycleError('coordination_access_denied')

    def execute(self, request_id, action, target, body, *, access, recover=False):
        if not self.enabled: raise LifecycleError('lifecycle_disabled')
        if type(action) is not str or action not in ACTIONS: raise LifecycleError('coordination_action_unsupported')
        if type(recover) is not bool: raise LifecycleError('coordination_invalid')
        if ((action == 'session.patch.v1' and not _session_id(target))
                or (action == 'loop.update.v1' and target is not None)):
            raise LifecycleError('coordination_invalid')
        # Deny before any file/DB or fallback-route access. A second check inside
        # the cooperative gate applies to replay, publication and recovery.
        self._guard(access, action, target)
        if type(self.render_mvp) is not bool or not callable(self._historical) or not callable(self._routes):
            raise LifecycleError('coordination_invalid')
        if type(body) is not dict: raise LifecycleError('coordination_invalid')
        body = json.loads(encode(body))
        routes = json.loads(encode(self._routes())) if action == 'loop.update.v1' else []
        if not isinstance(routes, list): raise LifecycleError('coordination_invalid')
        parameters = {'contract':'guiting.config.command.v1', 'principal':access.principal_id,
                      'action':action, 'target':target, 'body':body,
                      'render_mvp':self.render_mvp, 'fallback_sha':digest(encode(routes))}
        guard = lambda: self._guard(access, action, target)
        def mutate(legacy):
            if action == 'session.patch.v1':
                WebSessionProviderAuthority(legacy, historical_provider=self._historical).patch_session(target, body)
            else:
                try:
                    config = updated_config(legacy.load_config(), body,
                        render_mvp=self.render_mvp, fallback_routes=routes)
                except (LoopConfigMutationError, ValueError, TypeError, OverflowError):
                    raise LifecycleError('coordination_config_invalid') from None
                legacy.save_config(config)
        with self.session_lock:
            if recover:
                return self.coordinator.recover(request_id, expected_parameters=parameters, guard=guard)
            return self.coordinator.mutate(request_id, parameters, mutate, guard=guard)

    def patch_session(self, request_id, session_id, body, *, access, recover=False):
        return self.execute(request_id, 'session.patch.v1', session_id, body, access=access, recover=recover)

    def update_config(self, request_id, body, *, access, recover=False):
        return self.execute(request_id, 'loop.update.v1', None, body, access=access, recover=recover)
