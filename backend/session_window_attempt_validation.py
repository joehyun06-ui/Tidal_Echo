"""Opt-in fresh lifecycle/evidence validation around unknown attempt recording.

No dispatch permission, runtime adapter, receipt confirmation or startup hook.
The supplied authority must actually fence its runtime/evidence writers; this
composition cannot turn point observations into a cross-process atomic fence.
"""
from .g3_projection_contract import ProjectionError
from .session_lifecycle import LifecycleError, SessionLifecycle
from .session_window_attempts import WindowAttemptJournal


def record_validated_attempt(engine, journal, cid, request_id, *, packet_sha256,
                             expected_revision, access, enabled=False, _on_recorded=None):
    if enabled is not True:
        raise LifecycleError('lifecycle_disabled')
    if (type(engine) is not SessionLifecycle or type(journal) is not WindowAttemptJournal
            or not engine.enabled or not journal.enabled or engine.repository is not journal.repository):
        raise LifecycleError('lifecycle_attempt_binding_invalid')
    binding = journal._check(cid, request_id, access)
    try:
        with engine.authority.fence(cid, access=access) as snapshot:
            engine._snapshot(snapshot, cid)
            if snapshot.provider != 'api':
                raise LifecycleError('lifecycle_provider_unsupported')

            def validate(state):
                engine._bound(state, snapshot, binding)
                engine._current(state, snapshot)
                _, digest = engine._packet(state, snapshot, access)
                if digest != state.packet_sha256 or digest != packet_sha256:
                    raise LifecycleError('lifecycle_packet_changed')
                if state.revision != expected_revision:
                    raise LifecycleError('lifecycle_attempt_state_changed')
                if engine.authority.validate(snapshot, access=access) is not True:
                    raise LifecycleError('lifecycle_authority_changed')

            result = journal.record_attempt(cid, request_id, packet_sha256=packet_sha256,
                expected_revision=expected_revision, access=access, _guard=validate)
            # Failure here may follow a committed row: inspect the original ID,
            # never infer rollback or send/re-record under a new request ID.
            if engine.authority.validate(snapshot, access=access) is not True:
                raise LifecycleError('lifecycle_authority_changed_after_commit')
            if not result.replayed and _on_recorded is not None:
                _on_recorded(result)
            return result
    except ProjectionError:
        raise LifecycleError('lifecycle_evidence_unavailable') from None
    except OSError:
        raise LifecycleError('lifecycle_storage_unavailable') from None
