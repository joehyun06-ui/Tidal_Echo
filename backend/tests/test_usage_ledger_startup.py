"""Explicit disabled/history/capture selection over synthetic files only."""
from unittest.mock import patch

from backend.usage_ledger import LedgerError, build_view
from backend.usage_ledger_startup import prepare
from backend.tests.test_usage_ledger_composition import CompositionFixture, KEY, PREFIX_KEY, ROUTE, RAW, profile

ACTIVATION = '11111111-1111-4111-8111-111111111111'


class StartupTests(CompositionFixture):
    def options(self):
        return {'ledger_path': self.path, 'relay_db': self.relay, 'native_db': self.native, 'continuation_key': KEY}

    def start(self, mode='history', **kwargs):
        return prepare(mode=mode, activation_id=ACTIVATION, service_options={**self.options(), **kwargs})

    def test_default_disabled_is_exact_noop_without_inspecting_paths_or_options(self):
        with patch('backend.usage_ledger_startup.LedgerServices', side_effect=AssertionError('must not construct')):
            self.assertIsNone(prepare())
            self.assertIsNone(prepare(service_options=object(), activation_id=object()))
        self.assertFalse(self.path.exists())

    def test_invalid_selection_or_secret_fields_fail_before_creating_ledger(self):
        cases = [{'mode': 'on'}, {'mode': True}, {'mode': 'capture'},
                 {'mode': 'history', 'activation_id': 'not-an-id'},
                 {'mode': 'history', 'service_options': {**self.options(), 'api_key': 'synthetic-private'}}]
        for changes in cases:
            options = {'mode': 'history', 'activation_id': ACTIVATION, 'service_options': self.options(), **changes}
            with self.assertRaisesRegex(LedgerError, '^ledger_startup_invalid$'): prepare(**options)
            self.assertFalse(self.path.exists())

    def test_history_forbids_capture_configuration_instead_of_silently_ignoring_it(self):
        for changes in ({'api_profiles': [profile()]}, {'fingerprint_key': PREFIX_KEY},
                        {'codex_provider_id': 'subscription'}, {'exclusive_threads': True}):
            with self.assertRaisesRegex(LedgerError, 'startup_invalid'): self.start(**changes)
        self.assertFalse(self.path.exists())

    def test_history_keeps_no_collectors_and_pause_markers_are_idempotent(self):
        for _ in range(2):
            services = self.start()
            self.assertEqual(services.api_runtime_kwargs(), {}); self.assertEqual(services.codex_runtime_kwargs(), {})
        for mode, sid in [('api', 'a'), ('codex', 'b')]:
            health = build_view(services.ledger, mode, sid)['collection_health']
            self.assertEqual(health['capture_pauses'], 1)
            self.assertEqual(health['issues'], {}); self.assertEqual(health['unassigned_issues'], 0)
            self.assertTrue(health['has_observed_gaps'])

    async def test_capture_history_resume_preserves_rows_and_inflight_snapshot(self):
        first = self.start('capture', api_profiles=[profile()], fingerprint_key=PREFIX_KEY)
        old = first.api.begin(ROUTE, 'chat', 'a', 'inflight')
        history = self.start()
        self.assertIsNone(history.api)
        old.usage(RAW); await old.finish(None)
        resumed = self.start('capture', api_profiles=[profile()], fingerprint_key=PREFIX_KEY)
        new = resumed.api.begin(ROUTE, 'chat', 'a', 'after-resume'); new.usage(RAW); await new.finish(None)
        view = build_view(history.ledger, 'api', 'a')
        self.assertEqual(view['summary']['requests'], 2)
        self.assertEqual(view['summary']['metrics']['input']['value'], 200)
        self.assertEqual(view['collection_health']['capture_pauses'], 1)
        self.assertEqual([r['config_revision'] for r in resumed.ledger.records('api')], ['v1', 'v1'])

    async def test_pause_history_cannot_be_hidden_by_session_or_model_budget_filter(self):
        services = self.start('capture', api_profiles=[profile()], fingerprint_key=PREFIX_KEY)
        attempt = services.api.begin(ROUTE, 'chat', 'a', 'turn'); attempt.usage(RAW); await attempt.finish(None)
        services.ledger.save_settings('api', 0, {'threshold_tokens': None, 'warning_margin_tokens': None,
                                               'timezone': 'UTC', 'budgets': [{'currency': 'USD', 'period': 'day', 'limit': '100'}]})
        self.start()
        for sid, model in [('a', None), ('b', 'unknown-model')]:
            view = build_view(services.ledger, 'api', sid, model=model)
            self.assertEqual(view['collection_health']['capture_pauses'], 1)
            self.assertIsNotNone(view['budgets'][0]['known_spend'])
            self.assertIsNone(view['budgets'][0]['remaining']); self.assertFalse(view['budgets'][0]['complete'])

    async def test_history_mode_keeps_delete_hooks_and_cannot_resurrect_late_result(self):
        capture = self.start('capture', api_profiles=[profile()], fingerprint_key=PREFIX_KEY)
        late = capture.api.begin(ROUTE, 'chat', 'a', 'late')
        history = self.start(); history.bind_authority(self.authority, self.lock)
        self.assertTrue(history.delete_session('a')['deleted']['ledger_deleted'])
        late.usage(RAW); await late.finish(None)
        self.assertEqual(capture.ledger.records('api'), [])

    def test_disabled_rollback_does_not_delete_existing_ledger_or_pause_evidence(self):
        services = self.start()
        self.assertIsNone(prepare(mode='disabled'))
        self.assertTrue(self.path.exists())
        self.assertEqual(build_view(services.ledger, 'api', 'a')['collection_health']['capture_pauses'], 1)

    async def test_changing_to_native_only_capture_cannot_hide_stopped_api_coverage(self):
        api = self.start('capture', api_profiles=[profile()], fingerprint_key=PREFIX_KEY)
        attempt = api.api.begin(ROUTE, 'chat', 'a', 'before-mode-change'); attempt.usage(RAW); await attempt.finish(None)
        native = self.start('capture', codex_provider_id='synthetic-subscription', codex_cli_version='0.160.0')
        self.assertIsNone(native.api); self.assertIsNotNone(native.codex)
        view = build_view(native.ledger, 'api', 'a')
        self.assertEqual(view['summary']['requests'], 1)
        self.assertEqual(view['collection_health']['capture_pauses'], 1)
        self.assertTrue(view['collection_health']['has_observed_gaps'])
