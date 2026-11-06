"""Default-off fixed packets for API row publication and metadata-only merge."""
from pathlib import Path
import re

from .session_lifecycle import LifecycleError,_session_id
from .session_lifecycle_coordination import SharedSessionCoordinator,digest,encode
from .session_lifecycle_request_packets import PrivateCommandPackets,PacketReference
from .session_lifecycle_api_creation import CoordinatedApiSessionCommands,PUBLISH
from .session_lifecycle_session_lists import CoordinatedSessionListCommands,SAVE,IMMUTABLE


def fail(category='coordination_metadata_packet_invalid'):
    raise LifecycleError(category)


class _Binding:
    production_supported=False
    http_integrated=False
    native_integrated=False
    automatic_recovery_supported=False

    def __init__(self,commands,packets,*,session_lock,enabled=False):
        self.commands,self.packets,self.session_lock=commands,packets,session_lock
        self.enabled=enabled is True

    def _admit(self,target,access):
        if not self.enabled:fail('lifecycle_disabled')
        if type(self.commands) is not self.command_type or type(self.packets) is not PrivateCommandPackets:fail()
        self.commands._admit(access,self.action)
        if self.action==PUBLISH:
            if not _session_id(target):fail()
            self.commands._guard(access,self.action,[{'id':target,'provider':'api'}])
        elif target is not None:fail()
        self.commands.assert_session_lock(self.session_lock)
        c=self.commands.coordinator
        if type(c) is not SharedSessionCoordinator or c.canonical_path is None:fail('coordination_resource_mismatch')
        try:
            paths=[Path(c.canonical_path),Path(c.config),Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve()!=p for p in paths):fail('coordination_resource_mismatch')
        except (OSError,RuntimeError,TypeError,ValueError):
            raise LifecycleError('coordination_resource_mismatch') from None
        if self.packets.binding!=digest(encode([str(p) for p in paths])):fail('coordination_resource_mismatch')

    def _load(self,target,reference,access,recover):
        if type(reference) is not PacketReference or type(recover) is not bool:fail()
        p=self.packets.load(reference.request_id,self.action,target,expected_sha=reference.packet_sha,access=access)
        if (set(p)!={'contract','target','request_id','body','preconditions'}
                or p['contract']!=self.contract or p['target']!=target or p['request_id']!=reference.request_id
                or type(p['body']) is not dict or set(p['body'])!=self.body_keys
                or type(p['preconditions']) is not dict
                or set(p['preconditions'])!={'expected_generation','expected_snapshot_sha'}):fail()
        return p


class PrivateApiRowPublish(_Binding):
    command_type=CoordinatedApiSessionCommands
    action=PUBLISH
    contract='guiting.private-api-row-publish.v1'
    body_keys=frozenset({'row','activate'})

    def prepare(self,row,*,request_id,activate,access):
        sid=row.get('id') if type(row) is dict else None
        self._admit(sid,access)
        if type(activate) is not bool:fail()
        row=self.commands._prepare(row)
        with self.session_lock:
            snapshot=self.commands.snapshot_sessions(access=access)
            self.commands._guard(access,PUBLISH,[row])
            p={'contract':self.contract,'target':sid,'request_id':request_id,'body':{'row':row,'activate':activate},
                'preconditions':{'expected_generation':snapshot.generation,'expected_snapshot_sha':snapshot.snapshot_sha}}
            return self.packets.prepare(request_id,PUBLISH,sid,p,access=access)

    def execute(self,session_id,reference,*,access,recover=False):
        self._admit(session_id,access)
        with self.session_lock:
            p=self._load(session_id,reference,access,recover)
            row=self.commands._prepare(p['body']['row'])
            if row['id']!=session_id:fail()
            return self.commands.publish_row(row,activate=p['body']['activate'],request_id=reference.request_id,
                access=access,recover=recover,**p['preconditions'])


class PrivateSessionMetadataSave(_Binding):
    command_type=CoordinatedSessionListCommands
    action=SAVE
    contract='guiting.private-session-metadata-save.v1'
    body_keys=frozenset({'rows','active'})

    def prepare(self,rows,active=None,*,request_id,expected_generation,expected_snapshot_sha,access):
        self._admit(None,access)
        if (type(expected_generation) is not int or not 0<=expected_generation<2**63-1
                or type(expected_snapshot_sha) is not str or not re.fullmatch('[0-9a-f]{64}',expected_snapshot_sha)):fail()
        rows=self.commands._rows(rows)
        if active is not None and (not _session_id(active) or active not in {r['id'] for r in rows}):fail()
        self.commands._guard(access,SAVE,rows)
        with self.session_lock:
            snapshot=self.commands.snapshot_sessions(access=access)
            # Caller supplies the snapshot from which edits were made. Never
            # substitute a new hash for an old bulk metadata draft.
            if (snapshot.generation,snapshot.snapshot_sha)!=(expected_generation,expected_snapshot_sha):
                fail('coordination_cas_conflict')
            current=snapshot.as_rows()
            if len(rows)!=len(current) or any(any(a[k]!=b[k] for k in IMMUTABLE) for a,b in zip(rows,current)):
                fail('coordination_session_topology_unsupported')
            self.commands._guard(access,SAVE,rows)
            p={'contract':self.contract,'target':None,'request_id':request_id,'body':{'rows':rows,'active':active},
                'preconditions':{'expected_generation':expected_generation,'expected_snapshot_sha':expected_snapshot_sha}}
            return self.packets.prepare(request_id,SAVE,None,p,access=access)

    def execute(self,reference,*,access,recover=False):
        self._admit(None,access)
        with self.session_lock:
            p=self._load(None,reference,access,recover)
            return self.commands.save_sessions(reference.request_id,p['body']['rows'],p['body']['active'],
                access=access,recover=recover,**p['preconditions'])
