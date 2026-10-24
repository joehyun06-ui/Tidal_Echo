"""Credential-free configuration review and frozen production composition gates."""
import ast
import copy
import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.usage_ledger import LedgerError
from backend.usage_ledger_preflight import main, mock_plan, review_plan

ROOT = Path(__file__).resolve().parents[2]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.plan = mock_plan()
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('offline-only')))

    def test_consistent_declarations_never_mean_enabled_verified_or_complete(self):
        value = review_plan(self.plan)
        self.assertTrue(value['configuration_consistent'])
        self.assertFalse(value['production_verified'])
        self.assertFalse(value['enables_production'])
        self.assertIsNone(value['billing_complete'])
        self.assertEqual(value['basis'], 'supplied_plan_only')
        self.assertIn('explicit_enabling_decision', value['pending_acceptance'])
        self.assertEqual(value['api']['matched_route_count'], 2)
        self.assertEqual(value['api']['unpriced_profile_count'], 1)
        self.assertIn('other_codex_clients', value['excluded_sources'])

    def test_review_has_no_disk_or_environment_side_effects_and_preserves_input(self):
        before = copy.deepcopy(self.plan)
        with patch('pathlib.Path.open', side_effect=AssertionError('no files')), \
             patch('os.getenv', side_effect=AssertionError('no environment')), \
             patch('sqlite3.connect', side_effect=AssertionError('no databases')):
            review_plan(self.plan)
        self.assertEqual(self.plan, before)

    def test_fallback_or_changed_model_is_unmatched_without_exact_profile(self):
        for field, changed in (('model', 'new-model'), ('protocol', 'responses'),
                               ('endpoint', 'https://changed.invalid/v1')):
            plan = copy.deepcopy(self.plan)
            plan['api']['routes'].append({**plan['api']['routes'][0], field: changed})
            value = review_plan(plan)
            self.assertFalse(value['configuration_consistent'])
            self.assertEqual(value['api']['unmatched_route_indices'], [3])
            self.assertEqual(value['checks']['api_routes_matched'], 'missing_or_conflicting')

    def test_matching_uses_same_trailing_slash_rule_as_collector(self):
        self.plan['api']['routes'][0]['endpoint'] += '/'
        self.assertTrue(review_plan(self.plan)['configuration_consistent'])

    def test_extra_profile_is_disclosed_without_claiming_an_observation(self):
        profile = copy.deepcopy(self.plan['api']['profiles'][0])
        profile['model'] = 'unused'; profile['dialect'] = 'unknown'
        self.plan['api']['profiles'].append(profile)
        value = review_plan(self.plan)
        self.assertEqual(value['api']['unused_profile_count'], 1)
        self.assertEqual(value['api']['unknown_dialect_count'], 0)
        self.plan['api']['routes'][0]['model'] = 'unused'
        self.assertEqual(review_plan(self.plan)['api']['unknown_dialect_count'], 1)

    def test_missing_hooks_and_dynamic_inventory_remain_blockers(self):
        for hook in self.plan['hooks']:
            plan = copy.deepcopy(self.plan); plan['hooks'][hook] = False
            self.assertFalse(review_plan(plan)['configuration_consistent'])
        for name in ('fingerprint_key_configured', 'dynamic_routes_reviewed'):
            plan = copy.deepcopy(self.plan); plan['api'][name] = False
            self.assertFalse(review_plan(plan)['configuration_consistent'])

    def test_storage_must_be_lexically_persistent_and_separate(self):
        for ledger in ('/tmp/usage.sqlite', '/var/data/relay.sqlite', '/var/data/codex.sqlite', '/var/data'):
            plan = copy.deepcopy(self.plan); plan['storage']['ledger_db'] = ledger
            self.assertFalse(review_plan(plan)['configuration_consistent'])
        for ledger in ('relative.sqlite', '/var/data/../usage.sqlite', '/var/data/./usage.sqlite', '//var/data/a', '/var/data/a\x00'):
            plan = copy.deepcopy(self.plan); plan['storage']['ledger_db'] = ledger
            with self.assertRaisesRegex(LedgerError, '^ledger_preflight_invalid$'): review_plan(plan)

    def test_codex_schema_is_pinned_not_silently_latest(self):
        self.plan['codex']['cli_version'] = '9.9.9'
        value = review_plan(self.plan)
        self.assertFalse(value['configuration_consistent'])
        self.assertEqual(value['checks']['codex_schema_matches'], 'missing_or_conflicting')
        self.assertEqual(value['codex']['api_fees'], 'not_applicable')
        self.assertEqual(value['codex']['api_cache'], 'not_applicable')

    def test_nonexclusive_threads_can_observe_context_without_inventing_turn_usage(self):
        value = review_plan(self.plan)
        self.assertTrue(value['configuration_consistent'])
        self.assertEqual(value['codex']['single_turn_tokens'], 'disabled_without_exclusivity')
        self.plan['codex']['exclusive_threads'] = True
        value = review_plan(self.plan)
        self.assertEqual(value['codex']['single_turn_tokens'], 'requires_continuity_acceptance')
        self.assertFalse(value['production_verified'])

    def test_native_bindings_reconciliation_and_receipts_are_individual_declarations(self):
        for key in ('durable_binding', 'background_reconciliation', 'manual_compaction'):
            plan = copy.deepcopy(self.plan); plan['codex'][key] = False
            self.assertFalse(review_plan(plan)['configuration_consistent'])
        del self.plan['storage']['codex_db']
        self.assertFalse(review_plan(self.plan)['configuration_consistent'])

    def test_mode_specific_review_does_not_require_other_mode_configuration(self):
        self.plan['codex'] = None; del self.plan['storage']['codex_db']
        value = review_plan(self.plan)
        self.assertTrue(value['configuration_consistent'])
        self.assertIn('web_codex', value['unplanned_sources'])
        self.plan = mock_plan(); self.plan['api'] = None
        value = review_plan(self.plan)
        self.assertTrue(value['configuration_consistent'])
        self.assertEqual(value['api'], {'planned': False})
        self.assertEqual(value['unplanned_sources'], ['web_chat', 'web_mcp'])

    def test_missing_web_surface_and_external_tool_fees_are_explicit(self):
        self.plan['api']['routes'] = [self.plan['api']['routes'][0]]
        value = review_plan(self.plan)
        self.assertEqual(value['unplanned_sources'], ['web_mcp'])
        self.assertIn('mcp_tool_service_charges', value['excluded_charges'])

    def test_credentials_unknown_fields_and_unsupported_sources_are_rejected(self):
        for target in ('root', 'route', 'profile', 'cache'):
            plan = copy.deepcopy(self.plan)
            obj = {'root': plan, 'route': plan['api']['routes'][0], 'profile': plan['api']['profiles'][0],
                   'cache': plan['api']['profiles'][0].setdefault('cache', {})}[target]
            obj['key'] = 'synthetic-private-key'
            with self.assertRaisesRegex(LedgerError, '^ledger_preflight_invalid$'): review_plan(plan)
        self.plan['api']['routes'][0]['source'] = 'telegram'
        with self.assertRaises(LedgerError): review_plan(self.plan)

    def test_untrusted_values_duplicate_routes_profiles_and_bool_coercion_are_rejected(self):
        mutations = [lambda p: p['api']['profiles'].append(copy.deepcopy(p['api']['profiles'][0])),
                     lambda p: p['api']['routes'].append(copy.deepcopy(p['api']['routes'][0])),
                     lambda p: p['api'].update(fingerprint_key_configured='true'),
                     lambda p: p['codex'].update(exclusive_threads=1),
                     lambda p: p['codex'].update(max_pending=True),
                     lambda p: p['codex'].update(max_pending=129),
                     lambda p: p.update(schema_version=True),
                     lambda p: p.update(api=None, codex=None)]
        for change in mutations:
            plan = copy.deepcopy(self.plan); change(plan)
            with self.assertRaises(LedgerError): review_plan(plan)

    def test_diagnostics_do_not_echo_routes_paths_models_or_identifiers(self):
        value = json.dumps(review_plan(self.plan))
        for marker in ('synthetic.invalid', '/var/data', 'synthetic-model', 'synthetic-api', 'synthetic-codex'):
            self.assertNotIn(marker, value)

    def command(self, *args):
        output = io.StringIO()
        with redirect_stdout(output): code = main(args)
        return code, json.loads(output.getvalue())

    def test_mock_command_is_successful_but_not_an_enable_command(self):
        code, value = self.command('--mock')
        self.assertEqual(code, 0)
        self.assertFalse(value['enables_production'])
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
            main(['--mock', '--enable'])

    def test_command_distinguishes_missing_checks_and_redacted_invalid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)/'synthetic-private-path.json'
            self.plan['hooks']['session_delete'] = False
            config.write_text(json.dumps(self.plan))
            code, value = self.command('--config', str(config))
            self.assertEqual(code, 1); self.assertFalse(value['configuration_consistent'])
            for data in ('{"schema_version":1,"schema_version":1}', 'x'*65537, '{"key":"synthetic-private-key"}'):
                config.write_text(data)
                code, value = self.command('--config', str(config))
                self.assertEqual(code, 2)
                self.assertEqual(value['error'], 'ledger_preflight_invalid')
                self.assertNotIn('synthetic-private', json.dumps(value))
            code, value = self.command('--config', str(Path(tmp)/'missing-private-file'))
            self.assertEqual(code, 2); self.assertNotIn(tmp, json.dumps(value))

    def test_current_production_composition_stays_unmounted_and_uninjected(self):
        # Parse only source: importing the entrypoint would load secrets/workers/memory.
        names = {'ApiWebRuntime', 'StreamingCodexGenerationRuntime'}
        entry = ast.parse((ROOT/'examples/api_loop_codex_canary.py').read_text())
        constructors = [n for n in ast.walk(entry) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id in names]
        self.assertEqual({n.func.id for n in constructors}, names)
        for call in constructors:
            self.assertFalse(any(k.arg in {'usage_collector', 'compaction_collector'} for k in call.keywords))
        for file in ('examples/api_loop_codex_canary.py', 'backend/p3_codex_relay_app.py'):
            source = ast.parse((ROOT/file).read_text())
            for node in ast.walk(source):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith('backend.usage_ledger'))
                if isinstance(node, ast.Call):
                    self.assertFalse(any(k.arg in {'usage_ledger', 'continuation_creator', 'manual_compactions'} for k in node.keywords))
