"""Default-off private original packet adapter for API edit/regenerate only."""
from dataclasses import asdict,fields
from pathlib import Path

from .session_lifecycle import LifecycleError,_session_id
from .session_lifecycle_coordination import digest,encode
from .session_lifecycle_api_versions import CoordinatedApiVersionCommands,ApiVersionSnapshot,CREATE
from .session_lifecycle_request_packets import PrivateCommandPackets,PacketReference

CONTRACT='guiting.private-api-version-create.v1'
PRECONDITIONS=frozenset(f.name for f in fields(ApiVersionSnapshot))


def fail(category='coordination_version_packet_invalid'):
    raise LifecycleError(category)


class PrivateApiVersionCreate:
    production_supported=False
    http_integrated=False
    automatic_recovery_supported=False

    def __init__(self,commands,packets,*,session_lock,enabled=False):
        self.commands,self.packets,self.session_lock=commands,packets,session_lock
        self.enabled=enabled is True

    def _admit(self,root,access):
        if not self.enabled:fail('lifecycle_disabled')
        if type(self.commands) is not CoordinatedApiVersionCommands or type(self.packets) is not PrivateCommandPackets:fail()
        self.commands._admit(access,CREATE)
        if not _session_id(root):fail()
        self.commands._guard(access,CREATE,[{'id':root,'provider':'api'}])
        self.commands.assert_session_lock(self.session_lock)
        c=self.commands.coordinator
        try:
            paths=[Path(self.commands.canonical_path),Path(c.config),Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve()!=p for p in paths):fail()
        except (OSError,RuntimeError,TypeError,ValueError):
            raise LifecycleError('coordination_version_packet_invalid') from None
        if self.packets.binding!=digest(encode([str(p) for p in paths])):fail('coordination_resource_mismatch')

    def prepare(self,root,source,body,*,title,created_at,select=True,access):
        self._admit(root,access)
        body,row=self.commands._version_request(root,source,body,title,created_at,select)
        self.commands._guard(access,CREATE,[{'id':sid,'provider':'api'} for sid in sorted({root,source,row['id']})])
        with self.session_lock:
            snapshot=self.commands.snapshot_version(root,source,body,title=title,created_at=created_at,select=select,access=access)
            ids=set(snapshot.expected_group_ids)|{row['id']}
            self.commands._guard(access,CREATE,[{'id':sid,'provider':'api'} for sid in sorted(ids)])
            packet={'contract':CONTRACT,'root':root,'source':source,'body':body,'title':title,
                'created_at':created_at,'select':select,'preconditions':asdict(snapshot)}
            # The existing UUID-derived target is also the concrete operation ID.
            return self.packets.prepare(row['id'],CREATE,root,packet,access=access)

    def execute(self,root,reference,*,access,recover=False):
        self._admit(root,access)
        if type(reference) is not PacketReference or type(recover) is not bool:fail()
        with self.session_lock:
            packet=self.packets.load(reference.request_id,CREATE,root,expected_sha=reference.packet_sha,access=access)
            if (set(packet)!={'contract','root','source','body','title','created_at','select','preconditions'}
                    or packet['contract']!=CONTRACT or packet['root']!=root
                    or type(packet['preconditions']) is not dict or set(packet['preconditions'])!=PRECONDITIONS):fail()
            body,row=self.commands._version_request(root,packet['source'],packet['body'],packet['title'],packet['created_at'],packet['select'])
            if row['id']!=reference.request_id or body!=packet['body']:fail()
            # Concrete command rechecks current group/target permission, CAS and
            # pending-copy proof. Recovery never re-preflights changed history.
            return self.commands.create_api_version(root,packet['source'],body,title=packet['title'],
                created_at=packet['created_at'],select=packet['select'],access=access,recover=recover,**packet['preconditions'])
