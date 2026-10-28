"""Read-only preview of the proposed 1M context rollover profile.

This is an observation, not an instruction or an enabled runtime policy. It
cannot create sessions, prepare summaries, send messages, or interrupt turns.
Existing user warning settings and billing remain independent.
"""
from .usage_ledger import LedgerError, count, identifier, timestamp


CAPACITY = 1_000_000
STAGES = (
    ('prepare', 500_000),
    ('switch', 600_000),
    ('priority', 700_000),
    ('emergency', 800_000),
)


def preview(value, *, observed_at=None, all_sessions=False, unconfirmed=0):
    """Evaluate only a current, attributed, single-session observation.

    No model-name lookup, cumulative usage fallback, scaling to smaller models,
    wall-clock freshness claim, or implicit setting change is performed. The
    caller owns freshness relative to newer turns and compaction. Even a valid
    observation does not establish idle generation or authorize an action.
    """
    value = value if isinstance(value, dict) else {}
    used, limit = count(value.get('tokens')), count(value.get('limit'))
    result = {
        'profile': '1m_500k_600k_700k_800k',
        'profile_capacity_tokens': CAPACITY,
        'thresholds': dict(STAGES),
        'status': 'unavailable', 'reason': None, 'stage': None,
        'next_threshold_tokens': None, 'distance_to_next_tokens': None,
        'distance_to_switch_tokens': None,
        'automatic_prepare_enabled': False, 'automatic_switch_enabled': False,
        'basis': 'context_observation_only',
        'observation': {'tokens': used, 'limit': limit, 'source': None, 'observed_at': None},
    }

    def unavailable(reason):
        result['reason'] = reason
        return result

    if all_sessions:
        return unavailable('single_session_required')
    if count(unconfirmed) is None:
        return unavailable('pending_state_unavailable')
    if unconfirmed:
        return unavailable('unconfirmed_work')
    if used is None:
        return unavailable('context_unavailable')
    if limit is None or limit == 0:
        return unavailable('capacity_unavailable')
    if used > limit:
        return unavailable('context_exceeds_capacity')
    if limit < CAPACITY:
        return unavailable('capacity_below_profile')
    if value.get('stale') is not False:
        return unavailable('observation_not_current')
    try:
        source = identifier(value.get('source'))
    except LedgerError:
        return unavailable('source_unavailable')
    try:
        at = timestamp(observed_at if observed_at is not None else value.get('observed_at'))
    except LedgerError:
        return unavailable('observation_time_unavailable')
    result['observation'].update(source=source, observed_at=at)
    stage, next_threshold = 'normal', None
    for name, threshold in STAGES:
        if used < threshold:
            next_threshold = threshold
            break
        stage = name
    if next_threshold is None:
        next_threshold = limit if used < limit else None
    if used == limit:
        stage = 'capacity_reached'
    result.update(status='observed', stage=stage,
                  next_threshold_tokens=next_threshold,
                  distance_to_next_tokens=next_threshold-used if next_threshold is not None else None,
                  distance_to_switch_tokens=max(0, dict(STAGES)['switch']-used))
    return result
