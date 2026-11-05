"""GM-02E default-off model-only migration and receipt-bound compensation.

Existing materialized private config only. No env-route bootstrap, HTTP install,
runtime restart/probe/model call, or unconditional old-file restoration.
"""
import json
from pathlib import Path
import re

from .provider_model_migration import prepare_model_only_change, validate_model_request
from .session_lifecycle import LifecycleError
from .session_lifecycle_commands import authorize_config_command
from .session_lifecycle_coordination import CommittedOperationProof, digest, encode

MIGRATE = 'provider.model.migrate.v1'
ROLLBACK = 'provider.model.rollback.v1'


class CoordinatedModelCommands:
    production_supported = False
    all_writers_integrated = False
    http_integrated = False

    def __init__(self, coordinator, *, session_lock, authorize, enabled=False):
        self.coordinator, self.session_lock = coordinator, session_lock
        self._authorize, self.enabled = authorize, enabled is True

    def _admit(self, path, access, action, recover):
        if not self.enabled: raise LifecycleError('lifecycle_disabled')
        authorize_config_command(self._authorize, access, action, None)
        if type(recover) is not bool: raise LifecycleError('coordination_invalid')
        try:
            requested = Path(path)
            if (not requested.is_absolute() or '..' in requested.parts
                    or requested != self.coordinator.config):
                raise LifecycleError('coordination_resource_mismatch')
        except (TypeError, ValueError, AttributeError):
            raise LifecycleError('coordination_resource_mismatch') from None

    @staticmethod
    def _generation(value):
        if type(value) is not int or not 0 <= value < 2**63 - 2:
            raise LifecycleError('coordination_invalid')
        return value

    @staticmethod
    def _parameters(access, action, body):
        return {'contract':'guiting.model.command.v1', 'principal':access.principal_id,
                'action':action, 'target':'primary_materialized_route', 'body':body}

    def _run(self, request_id, parameters, expected_generation, access, callback, *, recover, proof=None):
        guard = lambda: authorize_config_command(self._authorize, access, parameters['action'], None)
        options = {'guard':guard, 'expected_generation':expected_generation, 'require_operation':proof}
        with self.session_lock:
            if recover:
                return self.coordinator.recover(request_id, expected_parameters=parameters, **options)
            return self.coordinator.mutate(request_id, parameters, callback, **options)

    def migrate_primary_provider_model(self, loop_config, payload, *, request_id, access,
                                       expected_generation, expected_model, recover=False):
        self._admit(loop_config, access, MIGRATE, recover)
        model = validate_model_request(payload)
        previous = validate_model_request({'model':expected_model})
        generation = self._generation(expected_generation)
        body = {'model':model, 'expected_generation':generation, 'expected_model':previous}
        parameters = self._parameters(access, MIGRATE, body)
        def mutate(legacy):
            config, observed_model = prepare_model_only_change(legacy.load_config(), {'model':model})
            if observed_model != previous: raise LifecycleError('coordination_cas_conflict')
            legacy.save_config(config)
        return self._run(request_id, parameters, generation, access, mutate, recover=recover)

    def rollback_provider_model(self, loop_config, payload, *, request_id, access, recover=False):
        self._admit(loop_config, access, ROLLBACK, recover)
        if (type(payload) is not dict or set(payload) != {'migration_request_id','migration_base_generation',
                                                        'previous_model','migrated_model'}):
            raise LifecycleError('coordination_invalid')
        payload = json.loads(encode(payload))
        original_id = payload['migration_request_id']
        if type(original_id) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', original_id):
            raise LifecycleError('coordination_invalid')
        base = self._generation(payload['migration_base_generation'])
        previous = validate_model_request({'model':payload['previous_model']})
        migrated = validate_model_request({'model':payload['migrated_model']})
        original = self._parameters(access, MIGRATE, {'model':migrated, 'expected_generation':base, 'expected_model':previous})
        proof = CommittedOperationProof(original_id, digest(encode(original)), base)
        parameters = self._parameters(access, ROLLBACK, payload)
        def mutate(legacy):
            config, observed_model = prepare_model_only_change(legacy.load_config(), {'model':previous})
            if observed_model != migrated: raise LifecycleError('coordination_cas_conflict')
            legacy.save_config(config)
        return self._run(request_id, parameters, base + 1, access, mutate, recover=recover, proof=proof)
