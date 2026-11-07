"""Default-off, fixed history-count setting recovery; no draft CAS or HTTP."""
from pathlib import Path

from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_commands import CoordinatedConfigCommands
from .session_lifecycle_coordination import SharedSessionCoordinator, digest, encode, parse
from .session_lifecycle_request_packets import PrivateCommandPackets, PacketReference

ACTION = 'loop.update.v1'
CONTRACT = 'guiting.private-history-setting.v1'


def fail(category='coordination_history_packet_invalid'):
    raise LifecycleError(category)


class PrivateHistorySetting:
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
        if sid is not None: fail()
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
        if (type(body) is not dict or set(body) != {'history_n'}
                or type(body['history_n']) is not int or not 0 <= body['history_n'] <= 200): fail()
        return parse(encode(body))

    def prepare(self, history_n, *, request_id, access):
        session_id = None
        body = {"history_n": history_n}
        self._admit(session_id, access)
        body = self._body(body)
        with self.session_lock:
            self.commands._guard(access, ACTION, session_id)
            # Retain only the explicit setting, not routes, credentials or config.
            # Current server policy and fallback identity are resolved by the command.
            p = {'contract': CONTRACT, 'session_id': session_id, 'request_id': request_id, 'body': body}
            return self.packets.prepare(request_id, ACTION, session_id, p, access=access)

    def execute(self, reference, *, access, recover=False):
        session_id = None
        self._admit(session_id, access)
        if type(reference) is not PacketReference or type(recover) is not bool: fail()
        with self.session_lock:
            p = self.packets.load(reference.request_id, ACTION, session_id,
                expected_sha=reference.packet_sha, access=access)
            if (set(p) != {'contract', 'session_id', 'request_id', 'body'} or p['contract'] != CONTRACT
                    or p['session_id'] != session_id or p['request_id'] != reference.request_id): fail()
            body = self._body(p['body'])
            return self.commands.update_config(reference.request_id, body, access=access, recover=recover)
