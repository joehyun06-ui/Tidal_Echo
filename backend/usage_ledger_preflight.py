"""Offline configuration review. Never mounts routes or starts a collector.

Input is a deliberately credential-free deployment *plan*, not a live inventory.
Successful checks describe declarations only; they cannot authorize production,
prove physical disk placement, notification continuity, or complete billing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath

from .usage_ledger import LedgerError, identifier
from .usage_ledger_capture import CachePolicy, RouteProfile

MAX_BYTES = 65536
CODEX_SCHEMA_VERSION = '0.160.0'
HOOKS = frozenset({'authenticated_routes', 'session_authority', 'message_counts',
                  'session_delete', 'version_delete', 'retention_reviewed', 'continuations'})
API_SOURCES = frozenset({'web_chat', 'web_mcp'})
EXCLUDED = ['pre_capture_history', 'telegram', 'channel_bridge', 'kelivo',
            'operit_share', 'background_jobs', 'direct_provider_calls', 'other_codex_clients']
PENDING = ['live_composition_review', 'physical_storage_and_backup_review',
           'configuration_change_inventory', 'observation_continuity_acceptance',
           'explicit_enabling_decision']


def invalid():
    raise LedgerError('ledger_preflight_invalid')


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        invalid()


def flags(value, names):
    fields(value, names)
    if any(type(v) is not bool for v in value.values()): invalid()


def path(value):
    # Lexical Linux/Render plan only. No stat, resolve, mkdir, SQLite, or env reads.
    if (not isinstance(value, str) or not value.startswith('/') or len(value) > 1024
            or value != value.strip() or '\\' in value or '//' in value
            or any(p in {'.', '..'} for p in value.split('/')) or '\x00' in value):
        invalid()
    return PurePosixPath(value)


def route(value):
    fields(value, {'endpoint', 'model', 'protocol'})
    # Reuse the exact route rules used by the collector, without constructing it.
    p = RouteProfile(**value, provider_id='preflight', dialect='unknown', revision='preflight')
    return p.endpoint.rstrip('/'), p.model, p.protocol


def review_plan(value):
    """Return redacted findings. All booleans are declarations, never evidence."""
    try:
        return _review(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise LedgerError('ledger_preflight_invalid') from None


def _review(value):
    fields(value, {'schema_version', 'storage', 'hooks', 'api', 'codex'})
    if type(value['schema_version']) is not int or value['schema_version'] != 1: invalid()
    if value['api'] is None and value['codex'] is None: invalid()
    flags(value['hooks'], HOOKS)
    storage = value['storage']
    fields(storage, {'persistent_root', 'ledger_db', 'relay_db'}, {'codex_db'})
    paths = {k: path(v) for k, v in storage.items()}
    root, ledger = paths['persistent_root'], paths['ledger_db']
    independent = all(ledger != p and ledger not in p.parents and p not in ledger.parents
                      for k, p in paths.items() if k not in {'ledger_db', 'persistent_root'})
    checks = {'ledger_under_persistent_root': root in ledger.parents,
              'ledger_separate_from_source_databases': independent, **value['hooks']}
    api = {'planned': False}
    if value['api'] is not None:
        spec = value['api']
        fields(spec, {'routes', 'profiles', 'fingerprint_key_configured', 'dynamic_routes_reviewed'})
        if any(type(spec[k]) is not bool for k in ('fingerprint_key_configured', 'dynamic_routes_reviewed')): invalid()
        if any(not isinstance(spec[k], list) or not 1 <= len(spec[k]) <= 128 for k in ('routes', 'profiles')): invalid()
        profiles = {}
        for item in spec['profiles']:
            fields(item, {'endpoint', 'model', 'protocol', 'provider_id', 'dialect', 'revision'}, {'cache', 'price'})
            cache = item.get('cache', {})
            fields(cache, set(), {'kind', 'ttl'})
            p = RouteProfile(**{**item, 'cache': CachePolicy(**cache)})
            key = (p.endpoint.rstrip('/'), p.model, p.protocol)
            if key in profiles: invalid()
            profiles[key] = p
        routes = []
        for item in spec['routes']:
            fields(item, {'source', 'endpoint', 'model', 'protocol'})
            if item['source'] not in API_SOURCES: invalid()
            key = route({k: v for k, v in item.items() if k != 'source'})
            if (item['source'], key) in routes: invalid()
            routes.append((item['source'], key))
        unmatched = [i for i, (_, key) in enumerate(routes, 1) if key not in profiles]
        used = {key for _, key in routes} & profiles.keys()
        api = {'planned': True, 'sources': sorted({s for s, _ in routes}),
               'declared_route_count': len(routes), 'matched_route_count': len(routes)-len(unmatched),
               'unmatched_route_indices': unmatched, 'unused_profile_count': len(profiles)-len(used),
               'unknown_dialect_count': sum(profiles[k].dialect == 'unknown' for k in used),
               'unpriced_profile_count': sum(profiles[k].price is None for k in used),
               'actual_usage_and_charges': 'requires_observation'}
        checks.update(api_routes_matched=not unmatched,
                      api_fingerprint_key_declared=spec['fingerprint_key_configured'],
                      api_dynamic_routes_reviewed=spec['dynamic_routes_reviewed'])
    codex = {'planned': False}
    if value['codex'] is not None:
        spec = value['codex']
        fields(spec, {'provider_id', 'cli_version', 'exclusive_threads', 'max_pending',
                      'durable_binding', 'background_reconciliation', 'manual_compaction'})
        identifier(spec['provider_id'])
        if not isinstance(spec['cli_version'], str): invalid()
        if type(spec['max_pending']) is not int or not 1 <= spec['max_pending'] <= 128: invalid()
        for k in ('exclusive_threads', 'durable_binding', 'background_reconciliation', 'manual_compaction'):
            if type(spec[k]) is not bool: invalid()
        native = paths.get('codex_db')
        checks.update(codex_schema_matches=spec['cli_version'] == CODEX_SCHEMA_VERSION,
                      codex_store_declared=native is not None,
                      codex_durable_binding=spec['durable_binding'],
                      codex_background_reconciliation=spec['background_reconciliation'],
                      codex_manual_compaction=spec['manual_compaction'])
        codex = {'planned': True, 'sources': ['web_codex'],
                 'single_turn_tokens': 'requires_continuity_acceptance' if spec['exclusive_threads'] else 'disabled_without_exclusivity',
                 'context': 'requires_native_observation',
                 'api_fees': 'not_applicable', 'api_cache': 'not_applicable'}
    return {'ok': True, 'contract_version': 1, 'basis': 'supplied_plan_only',
            'configuration_consistent': all(checks.values()),
            'checks': {k: 'declared' if v else 'missing_or_conflicting' for k, v in sorted(checks.items())},
            'api': api, 'codex': codex, 'excluded_sources': list(EXCLUDED),
            'unplanned_sources': sorted((API_SOURCES - set(api.get('sources', []))) |
                                        (set() if codex['planned'] else {'web_codex'})),
            'excluded_charges': ['mcp_tool_service_charges', 'non_token_fees', 'official_subscription_billing'],
            'production_verified': False, 'enables_production': False,
            'billing_complete': None, 'pending_acceptance': list(PENDING)}


def mock_plan():
    """Synthetic example, not production defaults or a deployable manifest."""
    r = {'endpoint': 'https://synthetic.invalid/v1', 'model': 'synthetic-model', 'protocol': 'chat'}
    return {'schema_version': 1,
            'storage': {'persistent_root': '/var/data', 'ledger_db': '/var/data/usage-ledger.sqlite',
                        'relay_db': '/var/data/relay.sqlite', 'codex_db': '/var/data/codex.sqlite'},
            'hooks': {k: True for k in sorted(HOOKS)},
            'api': {'routes': [{'source': s, **r} for s in sorted(API_SOURCES)],
                    'profiles': [{**r, 'provider_id': 'synthetic-api', 'dialect': 'openai_chat', 'revision': 'mock-v1'}],
                    'fingerprint_key_configured': True, 'dynamic_routes_reviewed': True},
            'codex': {'provider_id': 'synthetic-codex', 'cli_version': CODEX_SCHEMA_VERSION,
                      'exclusive_threads': False, 'max_pending': 32, 'durable_binding': True,
                      'background_reconciliation': True, 'manual_compaction': True}}


def unique_pairs(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj: invalid()
        obj[k] = v
    return obj


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--mock', action='store_true', help='review synthetic declarations only')
    source.add_argument('--config', help='credential-free proposed plan JSON; no live runtime is loaded')
    args = parser.parse_args(argv)
    try:
        if args.mock:
            plan = mock_plan()
        else:
            with Path(args.config).open('rb') as f: data = f.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES: invalid()
            plan = json.loads(data, object_pairs_hook=unique_pairs)
        report = review_plan(plan)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report['configuration_consistent'] else 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({'ok': False, 'error': 'ledger_preflight_invalid', 'enables_production': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
