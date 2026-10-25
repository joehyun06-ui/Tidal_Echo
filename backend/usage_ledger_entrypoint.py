"""Candidate entrypoint adapters. Only the explicit startup mode activates I/O.

Never loads os.environ itself, writes configuration, logs secrets, or starts a
runtime. The deployed environment remains the operator's separate decision.
"""
import json
from pathlib import Path

from .usage_ledger import LedgerError
from .usage_ledger_capture import CachePolicy, RouteProfile
from .usage_ledger_preflight import MAX_BYTES, review_plan, unique_pairs
from .usage_ledger_startup import prepare

MODE = 'GUITING_LEDGER_MODE'
PLAN = 'GUITING_LEDGER_PLAN'
ACTIVATION = 'GUITING_LEDGER_ACTIVATION_ID'
CONTINUATION_KEY = 'GUITING_LEDGER_CONTINUATION_KEY'
FINGERPRINT_KEY = 'GUITING_LEDGER_FINGERPRINT_KEY'


def selected_mode(environ):
    mode = environ.get(MODE, 'disabled')
    if not isinstance(mode, str) or mode not in {'disabled', 'history', 'capture'}:
        raise LedgerError('ledger_entrypoint_invalid')
    return mode


def secret(environ, name):
    value = environ.get(name)
    if (not isinstance(value, str) or not 32 <= len(value) <= 512 or not value.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise ValueError()
    return value.encode('ascii')


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts: raise ValueError()
    return path


def load_loop(environ, *, persistent_root, relay_db, native_db):
    mode = selected_mode(environ)
    if mode == 'disabled': return None
    try:
        root_path = absolute(persistent_root)
        root = root_path.resolve()
        plan_path = absolute(environ.get(PLAN))
        if (plan_path.suffix != '.json' or root not in plan_path.resolve().parents
                or any(p.is_symlink() for p in (plan_path, *plan_path.parents, root_path))):
            raise ValueError()
        with plan_path.open('rb') as f: data = f.read(MAX_BYTES+1)
        if len(data) > MAX_BYTES: raise ValueError()
        plan = json.loads(data, object_pairs_hook=unique_pairs)
        reviewed = review_plan(plan)
        if mode == 'capture' and not reviewed['configuration_consistent']: raise ValueError()
        storage = plan['storage']
        ledger = absolute(storage['ledger_db']).resolve()
        if absolute(storage['persistent_root']).resolve() != root or root not in ledger.parents:
            raise ValueError()
        if absolute(storage['relay_db']).resolve() != absolute(relay_db).resolve(): raise ValueError()
        if 'codex_db' in storage and absolute(storage['codex_db']).resolve() != absolute(native_db).resolve():
            raise ValueError()
        options = {'ledger_path': ledger, 'relay_db': relay_db, 'native_db': native_db,
                   'continuation_key': secret(environ, CONTINUATION_KEY)}
        if mode == 'capture':
            if plan['api'] is not None:
                options['api_profiles'] = [RouteProfile(**{**p, 'cache': CachePolicy(**p.get('cache', {}))})
                                           for p in plan['api']['profiles']]
                options['fingerprint_key'] = secret(environ, FINGERPRINT_KEY)
            if plan['codex'] is not None:
                c = plan['codex']
                options.update(codex_provider_id=c['provider_id'], codex_cli_version=c['cli_version'],
                               exclusive_threads=c['exclusive_threads'], max_pending=c['max_pending'])
        return prepare(mode=mode, activation_id=environ.get(ACTIVATION), service_options=options)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, RecursionError):
        # No path, route, plan contents, key, or original exception in startup logs.
        raise LedgerError('ledger_entrypoint_unavailable') from None


def install_relay(relay, environ, *, client_factory=None):
    mode = selected_mode(environ)
    if mode == 'disabled': return False
    try:
        from .usage_ledger_proxy import install
        return install(relay.app, enabled=True, check_auth=relay.check_auth,
                       base_url=relay.loop_base_url(), internal_token=relay.API_LOOP_INTERNAL_TOKEN,
                       **({'client_factory': client_factory} if client_factory is not None else {}))
    except Exception:
        raise LedgerError('ledger_entrypoint_unavailable') from None
