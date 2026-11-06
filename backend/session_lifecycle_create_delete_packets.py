"""Default-off fixed private packets for empty API creation and plain deletion."""
from dataclasses import asdict,fields
from pathlib import Path

from .session_lifecycle import LifecycleError,_session_id
from .session_lifecycle_coordination import SharedSessionCoordinator,digest,encode
from .session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from .session_lifecycle_api_creation import CoordinatedApiSessionCommands,CREATE
from .session_lifecycle_api_delete import CoordinatedApiDeleteCommands,ApiDeleteSnapshot,DELETE


def fail(category='coordination_basic_api_packet_invalid'):
    raise LifecycleError(category)


class _Binding:
    production_supported=False
    http_integrated=False
    automatic_recovery_supported=False

    def __init__(self,commands,packets,*,session_lock,enabled=False):
        self.commands,self.packets,self.session_lock=commands,packets,session_lock
        self.enabled=enabled is True

    def _admit(self,sid,access):
        if not self.enabled:fail('lifecycle_disabled')
        if type(self.commands) is not self.command_type or type(self.packets) is not PrivateCommandPackets:fail()
        self.commands._admit(access,self.action)
        if not _session_id(sid):fail()
        self.commands._guard(access,self.action,[{'id':sid,'provider':'api'}])
        self.commands.assert_session_lock(self.session_lock)
        c=self.commands.coordinator
        if type(c) is not SharedSessionCoordinator or c.canonical_path is None:fail('coordination_resource_mismatch')
        try:
            paths=[Path(c.canonical_path),Path(c.config),Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve()!=p for p in paths):fail('coordination_resource_mismatch')
        except (OSError,RuntimeError,TypeError,ValueError):
            raise LifecycleError('coordination_resource_mismatch') from None
        if (self.packets.binding!=digest(encode([str(p) for p in paths]))
                or (hasattr(self.commands,'canonical_path') and self.commands.canonical_path!=paths[0])):
            fail('coordination_resource_mismatch')

    def _load(self,sid,reference,access,recover):
        if type(reference) is not PacketReference or type(recover) is not bool:fail()
        p=self.packets.load(reference.request_id,self.action,sid,expected_sha=reference.packet_sha,access=access)
        if (set(p)!=self.packet_keys or p['contract']!=self.contract or p['session_id']!=sid
                or p['request_id']!=reference.request_id or type(p['preconditions']) is not dict
                or set(p['preconditions'])!=self.preconditions):fail()
        return p


class PrivateApiSessionCreate(_Binding):
    command_type=CoordinatedApiSessionCommands
    action=CREATE
    contract='guiting.private-api-session-create.v1'
    packet_keys=frozenset({'contract','session_id','request_id','title','created_at','activate','preconditions'})
    preconditions=frozenset({'expected_generation','expected_snapshot_sha'})

    def prepare(self,*,session_id,request_id,title,created_at,activate=True,access):
        self._admit(session_id,access)
        if type(activate) is not bool:fail()
        self.commands._prepare({'id':session_id,'title':title,'since_id':0,'created_at':created_at,'pinned':False,'provider':'api'})
        with self.session_lock:
            # Existing list snapshot supplies the command's metadata CAS, with
            # its own read scope. Actual DB pin is checked by _meta on that conn.
            snapshot=self.commands.snapshot_sessions(access=access)
            self.commands._guard(access,CREATE,[{'id':session_id,'provider':'api'}])
            p={'contract':self.contract,'session_id':session_id,'request_id':request_id,'title':title,
                'created_at':created_at,'activate':activate,'preconditions':{
                    'expected_generation':snapshot.generation,'expected_snapshot_sha':snapshot.snapshot_sha}}
            return self.packets.prepare(request_id,CREATE,session_id,p,access=access)

    def execute(self,session_id,reference,*,access,recover=False):
        self._admit(session_id,access)
        with self.session_lock:
            p=self._load(session_id,reference,access,recover)
            return self.commands.create_api_session(session_id=session_id,request_id=reference.request_id,
                title=p['title'],created_at=p['created_at'],activate=p['activate'],access=access,recover=recover,**p['preconditions'])


class PrivateApiSessionDelete(_Binding):
    command_type=CoordinatedApiDeleteCommands
    action=DELETE
    contract='guiting.private-api-session-delete.v1'
    packet_keys=frozenset({'contract','session_id','request_id','deleted_at','next_active','preconditions'})
    preconditions=frozenset(f.name for f in fields(ApiDeleteSnapshot))

    def prepare(self,session_id,*,request_id,deleted_at,next_active=None,access):
        self._admit(session_id,access)
        scopes=self.commands._packet(session_id,next_active,deleted_at)
        self.commands._guard(access,DELETE,scopes)
        with self.session_lock:
            snapshot=self.commands.snapshot_delete(session_id,deleted_at=deleted_at,next_active=next_active,access=access)
            self.commands._guard(access,DELETE,scopes)
            p={'contract':self.contract,'session_id':session_id,'request_id':request_id,
                'deleted_at':deleted_at,'next_active':next_active,'preconditions':asdict(snapshot)}
            return self.packets.prepare(request_id,DELETE,session_id,p,access=access)

    def execute(self,session_id,reference,*,access,recover=False):
        self._admit(session_id,access)
        with self.session_lock:
            p=self._load(session_id,reference,access,recover)
            return self.commands.delete_conversation(session_id,request_id=reference.request_id,
                deleted_at=p['deleted_at'],next_active=p['next_active'],access=access,recover=recover,**p['preconditions'])
