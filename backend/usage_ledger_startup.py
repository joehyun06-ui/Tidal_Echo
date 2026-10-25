"""Explicit startup selection, no env reads, app imports, runtime mutation or start.

Switch by rebuilding the reviewed composition, not by changing a running collector.
History mode preserves read/delete hooks and durable intentional-coverage markers.
"""
from contextlib import closing
import sqlite3
import uuid

from .usage_ledger import LedgerError
from .usage_ledger_composition import LedgerServices
from .usage_ledger_journal import CollectionJournal


def prepare(*, mode='disabled', activation_id=None, service_options=None):
    if mode == 'disabled':
        return None  # Exact default no-op, even with stale/invalid uninspected options.
    try:
        if mode not in {'history', 'capture'} or not isinstance(service_options, dict): raise ValueError()
        activation = uuid.UUID(activation_id).hex
        options = dict(service_options)
        allowed = {'ledger_path', 'relay_db', 'continuation_key', 'native_db', 'api_profiles',
                   'fingerprint_key', 'codex_provider_id', 'codex_cli_version', 'exclusive_threads', 'max_pending'}
        if set(options)-allowed: raise ValueError()
        capture_keys = {'api_profiles', 'fingerprint_key', 'codex_provider_id', 'codex_cli_version'}
        if mode == 'history' and (any(options.get(k) is not None for k in capture_keys) or options.get('exclusive_threads', False)):
            raise ValueError()
        if mode == 'capture' and options.get('api_profiles') is None and options.get('codex_provider_id') is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise LedgerError('ledger_startup_invalid') from None
    try:
        services = LedgerServices(**options)
    except TypeError:
        raise LedgerError('ledger_startup_invalid') from None
    try:
        if mode == 'history':
            # Reusing one activation UUID is idempotent. Do not erase this evidence
            # on resumption: the intentionally unobserved interval remains unknown.
            for run_mode in ('api', 'codex'):
                request = 'pause-'+activation
                with closing(services.ledger.connect()) as conn:
                    exists = conn.execute("SELECT 1 FROM usage_ledger_issues WHERE mode=? AND provider_id='ledger-startup' AND request_id=? AND reason='capture_paused'", (run_mode, request)).fetchone()
                if not exists:
                    CollectionJournal(services.ledger, run_mode, 'ledger-startup').issue('capture_paused', request_id=request)
    except (sqlite3.Error, OSError):
        raise LedgerError('ledger_startup_unavailable') from None
    return services
