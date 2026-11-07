"""Historical original-request observation only; never activate, send or retry."""
import hashlib

from .g3_projection import _valid_id
from .session_lifecycle import LifecycleError, _json
from .session_window_attempts import WindowAttemptJournal, _hex, _row


def inspect_recorded_activation(journal, cid, activation_request_id, *, attempt_request_id,
                                 packet_sha256, access, enabled=False):
    if enabled is not True:
        raise LifecycleError('lifecycle_disabled')
    if type(journal) is not WindowAttemptJournal or not _valid_id(activation_request_id) or not _hex(packet_sha256):
        raise LifecycleError('lifecycle_attempt_invalid')
    binding = journal._check(cid, attempt_request_id, access)
    with journal.repository.observation(cid) as tx:
        journal._bound(tx, binding)
        row = _row(tx.conn, cid, request_id=attempt_request_id)
        receipt = tx.receipt(activation_request_id)
        result = {'state': 'attempt_missing', 'historical': False, 'revision_applied': None,
                  'window_confirmed_at_apply': False, 'current_window_confirmed': False,
                  'reply_completed': False, 'retry_authorized': False}
        if row is None:
            if receipt is not None:
                raise LifecycleError('lifecycle_activation_status_conflict')
            return result
        journal._observation(row, binding, replayed=True)
        if row[3] != packet_sha256:
            raise LifecycleError('lifecycle_attempt_request_conflict')
        result['state'] = 'unconfirmed'
        if receipt is None:
            return result
        expected = hashlib.sha256(_json(['activate_recorded', cid, binding,
                                         [packet_sha256, attempt_request_id]])).hexdigest()
        if (receipt.input_sha256 != expected or receipt.revision_applied != row[4] + 1
                or receipt.phase_at_apply != 'active' or receipt.window_confirmed is not True
                or receipt.reply_completed is not False):
            raise LifecycleError('lifecycle_activation_status_conflict')
        result.update(state='historical_confirmed', historical=True,
                      revision_applied=receipt.revision_applied, window_confirmed_at_apply=True)
        return result
