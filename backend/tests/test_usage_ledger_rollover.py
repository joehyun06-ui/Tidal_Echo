"""Synthetic context observations only; no provider, model or runtime starts."""
import copy
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.usage_ledger import Ledger, build_view
from backend.usage_ledger_journal import CollectionJournal
from backend.usage_ledger_rollover import preview


AT = '2026-10-28T02:00:00Z'


def observation(tokens=600_000, **changes):
    return {'tokens': tokens, 'limit': 1_000_000, 'stale': False,
            'source': 'synthetic_context', 'observed_at': AT, **changes}


class PreviewTests(unittest.TestCase):
    def assert_disabled(self, value):
        self.assertIs(value['automatic_prepare_enabled'], False)
        self.assertIs(value['automatic_switch_enabled'], False)

    def test_boundaries_and_distances_use_current_context(self):
        cases = [
            (0, 'normal', 500_000, 500_000, 600_000),
            (499_999, 'normal', 500_000, 1, 100_001),
            (500_000, 'prepare', 600_000, 100_000, 100_000),
            (599_999, 'prepare', 600_000, 1, 1),
            (600_000, 'switch', 700_000, 100_000, 0),
            (699_999, 'switch', 700_000, 1, 0),
            (700_000, 'priority', 800_000, 100_000, 0),
            (799_999, 'priority', 800_000, 1, 0),
            (800_000, 'emergency', 1_000_000, 200_000, 0),
            (999_999, 'emergency', 1_000_000, 1, 0),
            (1_000_000, 'capacity_reached', None, None, 0),
        ]
        for used, stage, next_at, distance, switch_distance in cases:
            with self.subTest(used=used):
                value = preview(observation(used))
                self.assertEqual(value['status'], 'observed')
                self.assertEqual(value['stage'], stage)
                self.assertEqual(value['next_threshold_tokens'], next_at)
                self.assertEqual(value['distance_to_next_tokens'], distance)
                self.assertEqual(value['distance_to_switch_tokens'], switch_distance)
                self.assert_disabled(value)

    def test_missing_and_invalid_counts_never_become_zero(self):
        for used in [None, True, False, -1, 1.5, '600000', 2**53]:
            with self.subTest(used=used):
                value = preview(observation(used))
                self.assertEqual(value['reason'], 'context_unavailable')
                self.assertIsNone(value['stage'])
                self.assertIsNone(value['distance_to_switch_tokens'])
                self.assert_disabled(value)

    def test_capacity_must_be_reported_and_profile_is_not_scaled(self):
        for limit in [None, 0, True, '1000000', 2**53, 200_000, 999_999]:
            with self.subTest(limit=limit):
                value = preview(observation(100_000, limit=limit))
                self.assertEqual(value['status'], 'unavailable')
                self.assertIsNone(value['stage'])
                self.assertEqual(value['thresholds']['switch'], 600_000)

    def test_larger_reported_capacity_does_not_scale_thresholds(self):
        value = preview(observation(800_000, limit=2_000_000))
        self.assertEqual(value['stage'], 'emergency')
        self.assertEqual(value['thresholds']['switch'], 600_000)
        self.assertEqual(value['next_threshold_tokens'], 2_000_000)

    def test_over_capacity_is_inconsistent_not_an_automatic_action(self):
        value = preview(observation(1_000_001))
        self.assertEqual(value['reason'], 'context_exceeds_capacity')
        self.assertIsNone(value['stage'])
        self.assert_disabled(value)

    def test_freshness_requires_explicit_false(self):
        for stale in [None, True, 0, '', 'false']:
            value = preview(observation(stale=stale))
            self.assertEqual(value['reason'], 'observation_not_current')
            self.assertIsNone(value['distance_to_next_tokens'])

    def test_source_and_timestamp_cannot_be_invented(self):
        for source in [None, '', {}, 'not a source']:
            self.assertEqual(preview(observation(source=source))['reason'], 'source_unavailable')
        for at in [None, '', 'not-a-date', '2026-10-28T02:00:00']:
            self.assertEqual(preview(observation(observed_at=at))['reason'], 'observation_time_unavailable')

    def test_caller_can_supply_the_authoritative_event_time(self):
        value = observation()
        del value['observed_at']
        self.assertEqual(preview(value, observed_at=AT)['status'], 'observed')

    def test_pending_or_unknown_work_blocks_a_current_phase(self):
        for pending in [1, None, True, -1, '0']:
            value = preview(observation(), unconfirmed=pending)
            self.assertEqual(value['status'], 'unavailable')
            self.assertIsNone(value['stage'])
            self.assert_disabled(value)

    def test_all_sessions_never_inherits_a_single_session_context(self):
        self.assertEqual(preview(observation(), all_sessions=True)['reason'], 'single_session_required')

    def test_no_mutation_or_shared_threshold_dictionary(self):
        original = observation()
        before = copy.deepcopy(original)
        first = preview(original)
        first['thresholds']['switch'] = 1
        self.assertEqual(original, before)
        self.assertEqual(preview(original)['thresholds']['switch'], 600_000)

    def test_no_cumulative_or_model_name_fallback(self):
        value = preview({'total_tokens': 800_000, 'model': 'synthetic-1m-model', 'stale': False})
        self.assertEqual(value['reason'], 'context_unavailable')
        self.assertIsNone(value['observation']['tokens'])

    def test_network_is_not_needed_even_at_the_emergency_stage(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            value = preview(observation(800_000))
        self.assertEqual(value['stage'], 'emergency')
        self.assert_disabled(value)


class ReportPreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ledger = Ledger(Path(temp.name)/'ledger.sqlite')

    def add_context(self, *, mode='api', sid='s-1', event_id='context-1', at=AT, tokens=600_000, **changes):
        self.ledger.event({'event_id': mode+'-'+sid+'-'+event_id, 'session_id': sid, 'mode': mode,
                           'at': at, 'kind': 'context', 'status': 'observed',
                           'snapshot': observation(tokens, observed_at=at, **changes)})

    def test_api_codex_and_session_ownership_are_separate(self):
        self.add_context()
        self.add_context(mode='codex', tokens=500_000)
        self.add_context(sid='s-2', tokens=800_000)
        self.assertEqual(build_view(self.ledger, 'api', 's-1')['rollover_preview']['stage'], 'switch')
        self.assertEqual(build_view(self.ledger, 'codex', 's-1')['rollover_preview']['stage'], 'prepare')
        self.assertEqual(build_view(self.ledger, 'api', 's-2')['rollover_preview']['stage'], 'emergency')
        self.assertEqual(build_view(self.ledger, 'codex', 's-2')['rollover_preview']['status'], 'unavailable')
        value = build_view(self.ledger, 'api', 's-1', all_sessions=True)
        self.assertIsNone(value['context']['tokens'])
        self.assertEqual(value['rollover_preview']['reason'], 'single_session_required')

    def test_compaction_invalidates_preview_until_a_new_observation(self):
        self.add_context()
        self.ledger.event({'event_id': 'compact', 'session_id': 's-1', 'mode': 'api',
                           'at': '2026-10-28T02:01:00Z', 'kind': 'compaction', 'status': 'completed'})
        self.assertEqual(build_view(self.ledger, 'api', 's-1')['rollover_preview']['status'], 'unavailable')
        self.add_context(event_id='context-2', at='2026-10-28T02:02:00Z', tokens=10_000)
        self.assertEqual(build_view(self.ledger, 'api', 's-1')['rollover_preview']['stage'], 'normal')

    def test_report_stale_and_pending_work_withdraw_the_preview(self):
        self.add_context(stale=True)
        self.assertEqual(build_view(self.ledger, 'api', 's-1')['rollover_preview']['reason'], 'observation_not_current')
        self.add_context(event_id='context-2', at='2026-10-28T02:01:00Z')
        journal = CollectionJournal(self.ledger, 'api', 'synthetic-provider')
        journal.start({'mode': 'api', 'provider_id': 'synthetic-provider', 'request_id': 'pending',
                       'session_id': 's-1', 'at': '2026-10-28T02:02:00Z'})
        self.assertEqual(build_view(self.ledger, 'api', 's-1')['rollover_preview']['reason'], 'unconfirmed_work')

    def test_old_observation_cannot_survive_newer_record_without_context(self):
        from backend.tests.test_usage_ledger import record
        self.add_context()
        self.ledger.append(record(at='2026-10-28T02:01:00Z'))
        value = build_view(self.ledger, 'api', 's-1')
        self.assertTrue(value['context']['stale'])
        self.assertEqual(value['rollover_preview']['reason'], 'observation_not_current')

    def test_existing_settings_and_warning_are_not_replaced_by_the_proposal(self):
        policy = {'threshold_tokens': 650_000, 'warning_margin_tokens': 60_000,
                  'timezone': 'Asia/Shanghai', 'budgets': [{'period': 'day', 'currency': 'USD', 'limit': '5'}]}
        self.ledger.save_settings('api', 0, policy)
        self.add_context()
        before = self.ledger.settings('api')
        value = build_view(self.ledger, 'api', 's-1')
        self.assertEqual(value['context']['threshold_tokens'], 650_000)
        self.assertEqual(value['context']['distance_to_threshold'], 50_000)
        self.assertTrue(value['context']['warning'])
        self.assertEqual(value['rollover_preview']['thresholds']['switch'], 600_000)
        self.assertFalse(value['context']['automatic_switch_enabled'])
        self.assertEqual(self.ledger.settings('api'), before)

    def test_reading_preview_does_not_create_events_or_write_database(self):
        self.add_context(tokens=800_000)
        with closing(self.ledger.connect()) as conn:
            before = tuple(conn.iterdump())
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            for _ in range(3):
                value = build_view(self.ledger, 'api', 's-1')
                self.assertFalse(value['rollover_preview']['automatic_prepare_enabled'])
                self.assertFalse(value['rollover_preview']['automatic_switch_enabled'])
        with closing(self.ledger.connect()) as conn:
            self.assertEqual(tuple(conn.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
