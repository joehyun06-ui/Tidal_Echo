"""Default-off fixed model-only migration and receipt-bound rollback packets."""
from pathlib import Path
import re
from .provider_model_migration import validate_model_request
from .session_lifecycle import LifecycleError
from .session_lifecycle_commands import authorize_config_command
from .session_lifecycle_model_commands import CoordinatedModelCommands, MIGRATE, ROLLBACK
from .session_lifecycle_coordination import SharedSessionCoordinator, digest, encode, parse
from .session_lifecycle_request_packets import PrivateCommandPackets, PacketReference


def fail(category='coordination_model_packet_invalid'): raise LifecycleError(category)


class _Binding:
    production_supported=False
    http_integrated=False
    native_integrated=False
    automatic_recovery_supported=False

    def __init__(self,commands,packets,*,session_lock,enabled=False):
        self.commands,self.packets,self.session_lock=commands,packets,session_lock
        self.enabled=enabled is True

    def _admit(self,access):
        if not self.enabled:fail('lifecycle_disabled')
        if type(self.commands) is not CoordinatedModelCommands or type(self.packets) is not PrivateCommandPackets:fail()
        if not self.commands.enabled:fail('lifecycle_disabled')
        authorize_config_command(self.commands._authorize,access,self.action,None)
        if self.commands.session_lock is not self.session_lock:fail('coordination_lock_mismatch')
        c=self.commands.coordinator
        if type(c) is not SharedSessionCoordinator or c.canonical_path is None:fail('coordination_resource_mismatch')
        try:
            paths=[Path(c.canonical_path),Path(c.config),Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve()!=p for p in paths):fail('coordination_resource_mismatch')
        except (OSError,RuntimeError,TypeError,ValueError):
            raise LifecycleError('coordination_resource_mismatch') from None
        if self.packets.binding!=digest(encode([str(p) for p in paths])):fail('coordination_resource_mismatch')

    def _retain(self,request_id,body,access):
        body=self._body(body)
        with self.session_lock:
            self._admit(access)
            return self.packets.prepare(request_id,self.action,None,
                {'contract':self.contract,'request_id':request_id,'body':body},access=access)

    def execute(self,reference,*,access,recover=False):
        self._admit(access)
        if type(reference) is not PacketReference or type(recover) is not bool:fail()
        with self.session_lock:
            p=self.packets.load(reference.request_id,self.action,None,expected_sha=reference.packet_sha,access=access)
            if (set(p)!={'contract','request_id','body'} or p['contract']!=self.contract
                    or p['request_id']!=reference.request_id):fail()
            body=self._body(p['body'])
            return self._execute(reference.request_id,body,access,recover)


class PrivateModelMigration(_Binding):
    action=MIGRATE
    contract='guiting.private-model-migration.v1'

    def _body(self,b):
        if type(b) is not dict or set(b)!={'model','expected_model','expected_generation'}:fail()
        validate_model_request({'model':b['model']});validate_model_request({'model':b['expected_model']})
        self.commands._generation(b['expected_generation'])
        return parse(encode(b))

    def prepare(self,model,*,request_id,expected_model,expected_generation,access):
        self._admit(access)
        return self._retain(request_id,dict(model=model,expected_model=expected_model,expected_generation=expected_generation),access)

    def _execute(self,rid,b,access,recover):
        return self.commands.migrate_primary_provider_model(self.commands.coordinator.config,{'model':b['model']},
            request_id=rid,expected_model=b['expected_model'],expected_generation=b['expected_generation'],access=access,recover=recover)


class PrivateModelRollback(_Binding):
    action=ROLLBACK
    contract='guiting.private-model-rollback.v1'

    def _body(self,b):
        if type(b) is not dict or set(b)!={'migration_request_id','migration_base_generation','previous_model','migrated_model'}:fail()
        if type(b['migration_request_id']) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',b['migration_request_id']):fail()
        self.commands._generation(b['migration_base_generation'])
        for k in ('previous_model','migrated_model'):validate_model_request({'model':b[k]})
        return parse(encode(b))

    def prepare(self,payload,*,request_id,access):
        self._admit(access)
        return self._retain(request_id,payload,access)

    def _execute(self,rid,b,access,recover):
        return self.commands.rollback_provider_model(self.commands.coordinator.config,b,request_id=rid,access=access,recover=recover)
