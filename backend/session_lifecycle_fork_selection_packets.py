"""Default-off fixed private packet adapters for API branch and selection."""
from dataclasses import asdict,fields
from pathlib import Path

from .session_lifecycle import LifecycleError,_session_id
from .session_lifecycle_coordination import digest,encode
from .session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from .session_lifecycle_api_fork import CoordinatedApiForkCommands,ApiForkSnapshot,FORK
from .session_lifecycle_version_selection import CoordinatedVersionSelectionCommands,VersionSelectionSnapshot,SELECT


def fail(category='coordination_api_packet_invalid'):
    raise LifecycleError(category)


class _Binding:
    production_supported=False
    http_integrated=False
    automatic_recovery_supported=False

    def __init__(self,commands,packets,*,session_lock,enabled=False):
        self.commands,self.packets,self.session_lock=commands,packets,session_lock
        self.enabled=enabled is True

    def _admit(self,root,access):
        if not self.enabled:fail('lifecycle_disabled')
        if type(self.commands) is not self.command_type or type(self.packets) is not PrivateCommandPackets:fail()
        self.commands._admit(access,self.action)
        if not _session_id(root):fail()
        self.commands._guard(access,self.action,[{'id':root,'provider':'api'}])
        self.commands.assert_session_lock(self.session_lock)
        c=self.commands.coordinator
        try:
            paths=[Path(self.commands.canonical_path),Path(c.config),Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve()!=p for p in paths):fail()
        except (OSError,RuntimeError,TypeError,ValueError):
            raise LifecycleError('coordination_api_packet_invalid') from None
        if self.packets.binding!=digest(encode([str(p) for p in paths])):fail('coordination_resource_mismatch')

    def _retain(self,root,rid,packet,ids,access):
        self.commands._guard(access,self.action,[{'id':sid,'provider':'api'} for sid in sorted(set(ids))])
        return self.packets.prepare(rid,self.action,root,packet,access=access)

    def _load(self,root,reference,access,recover):
        if type(reference) is not PacketReference or type(recover) is not bool:fail()
        packet=self.packets.load(reference.request_id,self.action,root,expected_sha=reference.packet_sha,access=access)
        if (set(packet)!=self.packet_keys or packet['contract']!=self.contract or packet['root']!=root
                or type(packet['preconditions']) is not dict or set(packet['preconditions'])!=self.preconditions):fail()
        return packet


class PrivateApiFork(_Binding):
    command_type=CoordinatedApiForkCommands
    action=FORK
    contract='guiting.private-api-branch-fork.v1'
    packet_keys=frozenset({'contract','root','body','title','created_at','preconditions'})
    preconditions=frozenset(f.name for f in fields(ApiForkSnapshot))

    def prepare(self,source,body,*,title,created_at,access):
        self._admit(source,access)
        body,row=self.commands._request(source,body,title,created_at)
        self.commands._guard(access,FORK,[{'id':sid,'provider':'api'} for sid in sorted({source,row['id']})])
        with self.session_lock:
            snapshot=self.commands.snapshot_fork(source,body,title=title,created_at=created_at,access=access)
            packet={'contract':self.contract,'root':source,'body':body,'title':title,
                'created_at':created_at,'preconditions':asdict(snapshot)}
            return self._retain(source,row['id'],packet,{source,row['id']},access)

    def execute(self,source,reference,*,access,recover=False):
        self._admit(source,access)
        with self.session_lock:
            packet=self._load(source,reference,access,recover)
            body,row=self.commands._request(source,packet['body'],packet['title'],packet['created_at'])
            if body!=packet['body'] or row['id']!=reference.request_id:fail()
            return self.commands.fork_conversation(source,body,title=packet['title'],created_at=packet['created_at'],
                access=access,recover=recover,**packet['preconditions'])


class PrivateVersionSelection(_Binding):
    command_type=CoordinatedVersionSelectionCommands
    action=SELECT
    contract='guiting.private-api-version-selection.v1'
    packet_keys=frozenset({'contract','root','request_id','body','preconditions'})
    preconditions=frozenset(f.name for f in fields(VersionSelectionSnapshot))

    def prepare(self,root,body,*,request_id,access):
        self._admit(root,access);body=self.commands._selection_request(root,body)
        self.commands._guard(access,SELECT,[{'id':sid,'provider':'api'} for sid in sorted({root,body['version_id']})])
        with self.session_lock:
            snapshot=self.commands.snapshot_selection(root,body,access=access)
            packet={'contract':self.contract,'root':root,'request_id':request_id,'body':body,'preconditions':asdict(snapshot)}
            return self._retain(root,request_id,packet,snapshot.expected_group_ids,access)

    def execute(self,root,reference,*,access,recover=False):
        self._admit(root,access)
        with self.session_lock:
            packet=self._load(root,reference,access,recover)
            if packet['request_id']!=reference.request_id:fail()
            body=self.commands._selection_request(root,packet['body'])
            return self.commands.select_version(root,body,request_id=reference.request_id,access=access,
                recover=recover,**packet['preconditions'])
