"""Default-off, fixed single-session patch recovery; no draft CAS or HTTP."""
from pathlib import Path

from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_commands import CoordinatedConfigCommands
from .session_lifecycle_coordination import SharedSessionCoordinator, digest, encode, parse
from .session_lifecycle_request_packets import PrivateCommandPackets, PacketReference

ACTION = 'session.patch.v1'
CONTRACT = 'guiting.private-session-patch.v1'


def fail(category='coordination_patch_packet_invalid'):
    raise LifecycleError(category)


class PrivateSessionPatch:
    production_supported = False
    http_integrated = False
    native_integrated = False
    automatic_recovery_supported = False
    draft_cas_supported = False

    def __init__(self, commands, packets, *, session_lock, enabled=False):
        self.commands, self.packets, self.session_lock = commands, packets, session_lock
        self.enabled = enabled is True

    def _admit(self, sid, access):
        if not self.enabled: fail('lifecycle_disabled')
        if type(self.commands) is not CoordinatedConfigCommands or type(self.packets) is not PrivateCommandPackets: fail()
        if not self.commands.enabled: fail('lifecycle_disabled')
        # Current trusted command authorization precedes dependency/file access.
        self.commands._guard(access, ACTION, sid)
        if not _session_id(sid): fail()
        self.commands.assert_session_lock(self.session_lock)
        c = self.commands.coordinator
        if type(c) is not SharedSessionCoordinator or c.canonical_path is None: fail('coordination_resource_mismatch')
        try:
            paths = [Path(c.canonical_path), Path(c.config), Path(c.directory)]
            if any(not p.is_absolute() or '..' in p.parts or p.resolve() != p for p in paths):
                fail('coordination_resource_mismatch')
        except (OSError, RuntimeError, TypeError, ValueError):
            raise LifecycleError('coordination_resource_mismatch') from None
        if self.packets.binding != digest(encode([str(p) for p in paths])): fail('coordination_resource_mismatch')

    @staticmethod
    def _body(body):
        # Narrow adapter contract. No truthy coercion of booleans, privilege,
        # provider, arbitrary settings or client-selected command dispatch.
        if type(body) is not dict or not body or set(body) - {'title', 'pinned', 'active'}: fail()
        if 'title' in body and type(body['title']) is not str: fail()
        if any(k in body and type(body[k]) is not bool for k in ('pinned', 'active')): fail()
        return parse(encode(body))

    def prepare(self, session_id, body, *, request_id, access):
        self._admit(session_id, access)
        body = self._body(body)
        with self.session_lock:
            self.commands._guard(access, ACTION, session_id)
            # Retain an explicit field patch, never a whole list/config snapshot.
            # Existence, tombstone and provider validation remain execution-time.
            p = {'contract': CONTRACT, 'session_id': session_id, 'request_id': request_id, 'body': body}
            return self.packets.prepare(request_id, ACTION, session_id, p, access=access)

    def execute(self, session_id, reference, *, access, recover=False):
        self._admit(session_id, access)
        if type(reference) is not PacketReference or type(recover) is not bool: fail()
        with self.session_lock:
            p = self.packets.load(reference.request_id, ACTION, session_id,
                expected_sha=reference.packet_sha, access=access)
            if (set(p) != {'contract', 'session_id', 'request_id', 'body'} or p['contract'] != CONTRACT
                    or p['session_id'] != session_id or p['request_id'] != reference.request_id): fail()
            body = self._body(p['body'])
            return self.commands.patch_session(reference.request_id, session_id, body, access=access, recover=recover)
