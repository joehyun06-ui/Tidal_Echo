"""Default-off recorded-attempt binding around existing receipt activation.

No dispatch, receipt creation, runtime/source consumption inference or startup.
The supplied runtime remains responsible for authentic, durable read receipts.
"""
from .session_lifecycle import LifecycleError, SessionLifecycle
from .session_window_attempts import WindowAttemptJournal, _row


def activate_recorded_attempt(engine, journal, cid, activation_request_id, *,
                               attempt_request_id, packet_sha256, access, enabled=False):
    if enabled is not True:
        raise LifecycleError('lifecycle_disabled')
    if (type(engine) is not SessionLifecycle or type(journal) is not WindowAttemptJournal
            or not engine.enabled or not journal.enabled or engine.repository is not journal.repository):
        raise LifecycleError('lifecycle_attempt_binding_invalid')
    journal._check(cid, attempt_request_id, access)

    def guard(tx, state, snapshot, binding):
        # Same canonical transaction as the state and activation receipt, not
        # an independently opened journal read that could race or deadlock.
        journal._bound(tx, binding)
        row = _row(tx.conn, cid, request_id=attempt_request_id)
        if row is None:
            raise LifecycleError('lifecycle_attempt_missing')
        journal._observation(row, binding, replayed=True)
        expected_revision = row[4] + (1 if state.phase == 'active' else 0)
        if (state.phase not in ('prepared', 'active') or state.revision != expected_revision
                or state.closeout_id != row[1] or state.packet_sha256 != row[3]
                or packet_sha256 != row[3]):
            raise LifecycleError('lifecycle_attempt_state_changed')

    return engine.activate(cid, activation_request_id, packet_sha256=packet_sha256, access=access,
                           _attempt_guard=guard, _attempt_request_id=attempt_request_id)
