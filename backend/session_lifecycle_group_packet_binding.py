"""Default-off explicit private-packet adapter for API group deletion only."""
from dataclasses import asdict,fields
from pathlib import Path

from .session_lifecycle import LifecycleError,_session_id
from .session_lifecycle_coordination import digest,encode
from .session_lifecycle_api_group_delete import CoordinatedApiGroupDeleteCommands,ApiGroupDeleteSnapshot,DELETE
from .session_lifecycle_request_packets import PrivateCommandPackets,PacketReference

CONTRACT='guiting.private-api-group-delete.v1'
PRECONDITIONS=frozenset(f.name for f in fields(ApiGroupDeleteSnapshot))


def fail(category='coordination_group_packet_invalid'):
    raise LifecycleError(category)


class PrivateApiGroupDelete:
    production_supported=False
    http_integrated=False
    automatic_recovery_supported=False

    def __init__(self,commands,packets,*,session_lock,enabled=False):
        self.commands,self.packets,self.session_lock=commands,packets,session_lock
        self.enabled=enabled is True

    def _admit(self,root,access):
        if not self.enabled:fail('lifecycle_disabled')
        if type(self.commands) is not CoordinatedApiGroupDeleteCommands or type(self.packets) is not PrivateCommandPackets:fail()
        self.commands._admit(access,DELETE)
        if not _session_id(root):fail()
        self.commands._guard(access,DELETE,[{'id':root,'provider':'api'}])
        self.commands.assert_session_lock(self.session_lock)
        c=self.commands.coordinator
        try:
            paths=[Path(self.commands.canonical_path),Path(c.config),Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve()!=p for p in paths):fail()
        except (OSError,RuntimeError,TypeError,ValueError):
            raise LifecycleError('coordination_group_packet_invalid') from None
        if self.packets.binding!=digest(encode([str(p) for p in paths])):fail('coordination_resource_mismatch')

    def prepare(self,root,*,request_id,deleted_at,next_active=None,access):
        self._admit(root,access)
        with self.session_lock:
            snapshot=self.commands.snapshot_group_delete(root,deleted_at=deleted_at,next_active=next_active,access=access)
            # Persist only a fully scoped preflight. Saving does not erase data.
            ids=set(snapshot.expected_group_ids)|({next_active} if next_active else set())
            self.commands._guard(access,DELETE,[{'id':sid,'provider':'api'} for sid in sorted(ids)])
            packet={'contract':CONTRACT,'root':root,'request_id':request_id,'deleted_at':deleted_at,
                'next_active':next_active,'preconditions':asdict(snapshot)}
            return self.packets.prepare(request_id,DELETE,root,packet,access=access)

    def execute(self,root,reference,*,access,recover=False):
        self._admit(root,access)
        if type(reference) is not PacketReference or type(recover) is not bool:fail()
        with self.session_lock:
            packet=self.packets.load(reference.request_id,DELETE,root,expected_sha=reference.packet_sha,access=access)
            if (set(packet)!={'contract','root','request_id','deleted_at','next_active','preconditions'}
                    or packet['contract']!=CONTRACT or packet['root']!=root or packet['request_id']!=reference.request_id
                    or type(packet['preconditions']) is not dict or set(packet['preconditions'])!=PRECONDITIONS):fail()
            # No body-supplied access/recover/dispatch; the concrete command
            # independently validates all original scopes, CAS and effect proof.
            return self.commands.delete_api_group(root,request_id=reference.request_id,access=access,
                deleted_at=packet['deleted_at'],next_active=packet['next_active'],recover=recover,**packet['preconditions'])
