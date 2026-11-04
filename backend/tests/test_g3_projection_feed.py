"""Bounded synthetic feed/index tests. No live credentials, DB, network or model."""
from dataclasses import replace
import inspect
import socket
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from backend.g3_projection import verify_projection_card
from backend.g3_projection_contract import AccessScope, ProjectionError
from backend.g3_projection_feed import (
    G3FeedPage, G3Scan, PagedG3ReadOnlySourceAdapter, RebuildableProjectionIndex, _binding,
)
from backend.tests.test_g3_projection import SyntheticG3ReadOnly


class SyntheticPagedG3(SyntheticG3ReadOnly):
    """Mutable harness creates synthetic authority. Consumer only receives reads."""
    def __init__(self):
        super().__init__()
        self.epoch = 0
        self.authorization = 0
        self.generation = 'synthetic-source-1'
        self.scans = {}
        self.page_hook = None
        self.validate_hook = None
        self.opened_revisions = []

    def add_revision(self, eid, body, **kwargs):
        previous = self._heads.get(eid)
        event = super().add_revision(eid, body, **kwargs)
        self.epoch += 1
        if previous is not None and previous.ref.scope != event.ref.scope:
            self.authorization += 1
        return event

    def deny(self, principal, scope):
        super().deny(principal, scope)
        self.authorization += 1

    def allow(self, principal, scope):
        self._denied.discard((principal, scope))
        self.authorization += 1

    def begin_scan(self, *, after_cursor, access):
        scan = G3Scan('scan_' + str(len(self.scans) + 1), self.generation,
                      'source_' + str(self.epoch), 'acl_' + str(self.authorization),
                      _binding(access), after_cursor,
                      max((e.cursor for e in self._timeline), default=0))
        visible = {eid for eid, head in self._heads.items()
                   if head.ref.scope in access.allowed_scopes
                   and (access.principal_id, head.ref.scope) not in self._denied}
        self.scans[scan.scan_id] = (tuple(self._timeline), visible)
        return scan

    def read_page(self, scan, *, after_cursor, limit, access):
        timeline, visible = self.scans[scan.scan_id]
        interval = tuple(e for e in timeline if after_cursor < e.cursor <= scan.upper_cursor)[:limit]
        through = interval[-1].cursor if interval else scan.upper_cursor
        # Current visibility governs every historical revision of an event.
        events = tuple(e for e in interval if e.ref.event_id in visible
                       and e.ref.scope in access.allowed_scopes)
        page = G3FeedPage(scan.scan_id, after_cursor, through, events, through == scan.upper_cursor)
        if self.page_hook:
            return self.page_hook(page)
        return page

    def validate_scan(self, scan, *, access):
        if self.validate_hook:
            self.validate_hook()
        return (scan.source_generation == self.generation
                and scan.source_epoch == 'source_' + str(self.epoch)
                and scan.authorization_epoch == 'acl_' + str(self.authorization)
                and scan.access_binding == _binding(access))

    def open_source(self, ref, *, access):
        self.opened_revisions.append((ref.event_id, ref.revision))
        return super().open_source(ref, access=access)


class ProjectionFeedTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SyntheticPagedG3()
        self.ctx = AccessScope('synthetic_principal', frozenset({'private_A', 'private_B'}))
        self.index = RebuildableProjectionIndex()
        self.adapter.add_revision('event_A', 'synthetic original', cursor=1)
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')))

    def refresh(self, **kwargs):
        return self.index.refresh(self.adapter, access=self.ctx, **kwargs)

    def read(self, eid='event_A'):
        return self.index.read(self.adapter, eid, access=self.ctx)

    def progress(self):
        return self.index.progress(access=self.ctx)

    def rejects(self, fn, category=None):
        with self.assertRaises(ProjectionError) as caught:
            fn()
        if category:
            self.assertEqual(caught.exception.category, category)
        self.assertEqual(str(caught.exception), caught.exception.category)
        for private in ('event_A', 'synthetic original', 'synthetic_principal'):
            self.assertNotIn(private, str(caught.exception))

    def test_three_layers_and_source_bytes_preserved(self):
        self.adapter.add_revision('event_B', 'synthetic pending', cursor=2, layer='short_term')
        self.adapter.add_revision('event_C', 'synthetic approved', cursor=3, layer='fixed', reviewed=True)
        source = dict(self.adapter._sources)
        result = self.refresh(page_size=1)
        self.assertEqual((result.pages, result.projection_count, result.cursor), (3, 3, 3))
        self.assertEqual([self.read(e).layer for e in ('event_A', 'event_B', 'event_C')],
                         ['long_term', 'short_term', 'fixed'])
        self.assertEqual(self.adapter._sources, source)
        self.assertEqual(self.read().authority, 'derived')

    def test_clear_and_rebuild_preserve_ids_and_source_refs(self):
        self.refresh()
        first = self.read()
        self.index.clear(access=self.ctx)
        self.assertEqual(self.progress().cursor, 0)
        self.rejects(self.read, 'projection_not_indexed')
        self.refresh()
        self.assertEqual(self.read(), first)

    def test_page_size_does_not_change_rebuilt_cards(self):
        for n in range(2, 5):
            self.adapter.add_revision('event_' + str(n), 'synthetic ' + str(n), cursor=n)
        self.refresh(page_size=1)
        first = dict(self.index._cards)
        self.index.clear(access=self.ctx)
        self.refresh(page_size=4)
        self.assertEqual(self.index._cards, first)

    def test_revisions_across_pages_only_open_latest_source(self):
        self.adapter.add_revision('event_A', 'synthetic revised', revision=2, cursor=2)
        result = self.refresh(page_size=1)
        self.assertEqual(result.changed_events, 1)
        self.assertEqual(self.adapter.opened_revisions, [('event_A', 2)])
        self.assertEqual(self.read().projection_text, 'synthetic revised')

    def test_incremental_revision_replaces_old_projection(self):
        self.refresh()
        old = self.read()
        self.adapter.add_revision('event_A', 'synthetic next', revision=2, cursor=2)
        self.refresh()
        self.assertNotEqual(self.read().projection_card_id, old.projection_card_id)
        self.assertEqual(self.progress().projection_count, 1)
        self.rejects(lambda: verify_projection_card(self.adapter, old, access=self.ctx))

    def test_revocation_evicts_without_opening_tombstone_bytes(self):
        self.refresh()
        before = self.adapter.open_reads
        self.adapter.add_revision('event_A', b'', revision=2, cursor=2, status='revoked')
        self.refresh()
        self.assertEqual(self.adapter.open_reads, before)
        self.assertEqual(self.progress().projection_count, 0)
        self.rejects(self.read, 'projection_not_indexed')

    def test_fixed_anchor_revocation_does_not_require_new_anchor_approval(self):
        self.adapter.add_revision('event_fixed', 'synthetic anchor', cursor=2, layer='fixed', reviewed=True)
        self.refresh()
        self.adapter.add_revision('event_fixed', b'', revision=2, cursor=3,
                                  status='revoked', layer='fixed', reviewed=False)
        self.refresh()
        self.rejects(lambda: self.read('event_fixed'), 'projection_not_indexed')

    def test_read_checks_acl_without_waiting_for_refresh(self):
        self.refresh()
        self.adapter.deny(self.ctx.principal_id, 'private_A')
        self.rejects(self.read, 'projection_access_denied')
        self.assertEqual(self.progress().projection_count, 0)

    def test_authorization_epoch_rebuild_removes_invisible_cached_cards(self):
        self.adapter.add_revision('event_B', 'synthetic B', scope='private_B', cursor=2)
        self.refresh()
        self.adapter.deny(self.ctx.principal_id, 'private_A')
        result = self.refresh(page_size=1)
        self.assertTrue(result.rebuilt_for_authorization)
        self.assertEqual(self.progress().projection_count, 1)
        self.rejects(self.read, 'projection_not_indexed')
        self.assertEqual(self.read('event_B').source_cursor, 2)

    def test_new_grant_rebuild_can_recover_older_cursor_event(self):
        self.adapter.deny(self.ctx.principal_id, 'private_A')
        self.adapter.add_revision('event_B', 'synthetic B', scope='private_B', cursor=2)
        self.refresh()
        self.adapter.allow(self.ctx.principal_id, 'private_A')
        result = self.refresh()
        self.assertTrue(result.rebuilt_for_authorization)
        self.assertEqual(self.read().source_cursor, 1)

    def test_event_visibility_move_requires_epoch_and_rebuild(self):
        self.refresh()
        self.adapter.add_revision('event_A', 'synthetic private C', revision=2, cursor=2, scope='private_C')
        self.assertTrue(self.refresh().rebuilt_for_authorization)
        self.rejects(self.read, 'projection_not_indexed')

    def test_principal_and_scope_contexts_cannot_reuse_an_index(self):
        self.refresh()
        for context in (AccessScope('other', self.ctx.allowed_scopes),
                        AccessScope(self.ctx.principal_id, frozenset({'private_A'}))):
            self.rejects(lambda: self.index.refresh(self.adapter, access=context),
                         'projection_access_binding_mismatch')
            self.rejects(lambda: self.index.read(self.adapter, 'event_A', access=context),
                         'projection_access_binding_mismatch')

    def test_permission_change_during_scan_never_commits_a_partial_result(self):
        self.refresh()
        before = self.progress()
        self.adapter.add_revision('event_B', 'synthetic new', cursor=2)
        self.adapter.validate_hook = lambda: self.adapter.deny(self.ctx.principal_id, 'private_A')
        self.rejects(self.refresh, 'projection_scan_changed')
        self.assertEqual(self.progress(), before)
        self.rejects(self.read, 'projection_access_denied')

    def test_source_changes_during_materialization_are_rejected(self):
        self.adapter._replace_on_open = lambda: self.adapter.add_revision(
            'event_A', 'synthetic concurrent', revision=2, cursor=2)
        self.rejects(self.refresh)
        self.assertEqual(self.progress().cursor, 0)
        self.assertEqual(self.progress().projection_count, 0)

    def test_page_failure_leaves_previous_cursor_and_cards_together(self):
        self.refresh()
        before = (self.progress(), dict(self.index._cards))
        self.adapter.add_revision('event_B', 'synthetic new', cursor=2)
        self.adapter.add_revision('event_C', 'synthetic next', cursor=3)
        def fail_second(page):
            if page.after_cursor == 2:
                raise ProjectionError('projection_source_unavailable')
            return page
        self.adapter.page_hook = fail_second
        self.rejects(lambda: self.refresh(page_size=1))
        self.assertEqual((self.progress(), self.index._cards), before)
        self.adapter.page_hook = None
        self.assertEqual(self.refresh().projection_count, 3)

    def test_duplicate_and_skipped_page_boundaries_are_rejected(self):
        self.adapter.add_revision('event_B', 'synthetic B', cursor=2)
        for wrong in (0, 2):
            def hook(page):
                return replace(page, after_cursor=wrong) if page.after_cursor == 1 else page
            self.adapter.page_hook = hook
            self.rejects(lambda: self.refresh(page_size=1), 'projection_feed_invalid')
            self.assertEqual(self.progress().cursor, 0)

    def test_out_of_order_duplicate_cursor_and_revision_regression_rejected(self):
        self.adapter.add_revision('event_B', 'synthetic B', cursor=2)
        self.adapter.page_hook = lambda p: replace(p, events=tuple(reversed(p.events)))
        self.rejects(self.refresh)
        self.adapter.page_hook = lambda p: replace(p, events=(p.events[0], p.events[0]))
        self.rejects(self.refresh)
        self.adapter.page_hook = None
        self.adapter.add_revision('event_A', 'synthetic invalid revision', revision=1, cursor=3)
        self.rejects(self.refresh)

    def test_early_done_and_nonprogress_pages_rejected(self):
        self.adapter.add_revision('event_B', 'synthetic B', cursor=2)
        for change in ({'done': True}, {'next_cursor': 0, 'events': ()},
                       {'next_cursor': 2, 'done': False}):
            self.adapter.page_hook = lambda p: replace(p, **change)
            self.rejects(lambda: self.refresh(page_size=1))

    def test_wrong_scan_page_types_and_boolean_cursors_rejected(self):
        for change in ({'scan_id': 'different'}, {'after_cursor': False}, {'next_cursor': True},
                       {'events': []}, {'done': 1}):
            self.adapter.page_hook = lambda p: replace(p, **change)
            self.rejects(self.refresh)

    def test_empty_acl_filtered_page_advances_trusted_interval_cursor(self):
        self.adapter.add_revision('event_hidden', 'synthetic hidden', scope='private_C', cursor=2)
        self.adapter.add_revision('event_B', 'synthetic B', cursor=3)
        result = self.refresh(page_size=1)
        self.assertEqual((result.pages, result.cursor, result.projection_count), (3, 3, 2))

    def test_scan_budgets_fail_without_publishing_or_advancing(self):
        self.adapter.add_revision('event_B', 'synthetic B', cursor=2)
        for kwargs in ({'page_size': 1, 'max_pages': 1}, {'max_changes': 1}):
            self.rejects(lambda: self.refresh(**kwargs), 'projection_scan_budget_exceeded')
            self.assertEqual(self.progress().cursor, 0)
            self.assertEqual(self.progress().projection_count, 0)

    def test_invalid_budgets_are_rejected_before_adapter_reads(self):
        with patch.object(self.adapter, 'begin_scan', side_effect=AssertionError('no source access')):
            for kwargs in ({'page_size': True}, {'page_size': 201}, {'max_pages': 0},
                           {'max_pages': 1001}, {'max_changes': '1'}):
                self.rejects(lambda: self.refresh(**kwargs), 'projection_scan_budget_invalid')

    def test_adapter_must_not_return_more_events_than_requested(self):
        self.adapter.add_revision('event_B', 'synthetic B', cursor=2)
        self.adapter.page_hook = lambda p: replace(p, events=p.events + p.events)
        self.rejects(lambda: self.refresh(page_size=1))

    def test_scan_head_mismatch_rejected_before_source_bytes_opened(self):
        head = self.adapter._heads['event_A']
        with patch.object(self.adapter, 'read_event', return_value=replace(head, cursor=99)):
            self.rejects(self.refresh, 'projection_scan_changed')
        self.assertEqual(self.adapter.open_reads, 0)

    def test_source_and_cached_card_tampering_are_rejected_on_every_read(self):
        self.refresh()
        self.adapter._wrong_bytes['event_A'] = b'synthetic substitution'
        self.rejects(self.read, 'projection_imprint_mismatch')
        self.assertEqual(self.progress().projection_count, 0)
        self.adapter._wrong_bytes.clear()
        self.index.clear(access=self.ctx)
        self.refresh()
        self.index._cards['event_A'] = replace(self.index._cards['event_A'], projection_text='synthetic forged')
        self.rejects(self.read, 'projection_card_tampered_or_stale')

    def test_final_scan_validation_must_be_boolean_true(self):
        with patch.object(self.adapter, 'validate_scan', return_value=1):
            self.rejects(self.refresh, 'projection_scan_changed')

    def test_source_generation_change_requires_explicit_clear(self):
        self.refresh()
        self.adapter.generation = 'synthetic-source-2'
        self.rejects(self.refresh, 'projection_source_generation_changed')
        self.rejects(self.read, 'projection_source_generation_changed')
        self.index.clear(access=self.ctx)
        self.refresh()
        self.assertEqual(self.read().authority, 'derived')

    def test_observed_cursor_rollback_is_rejected(self):
        self.refresh()
        self.adapter._timeline.clear()
        self.rejects(self.refresh, 'projection_feed_invalid')
        self.assertEqual(self.progress().cursor, 1)

    def test_repeated_refresh_is_idempotent_and_cursor_does_not_move(self):
        self.refresh()
        before = dict(self.index._cards)
        result = self.refresh()
        self.assertEqual((result.changed_events, result.cursor), (0, 1))
        self.assertEqual(self.index._cards, before)

    def test_empty_authority_can_be_rebuilt_at_zero_cursor(self):
        empty = SyntheticPagedG3()
        result = self.index.refresh(empty, access=self.ctx)
        self.assertEqual((result.cursor, result.pages, result.projection_count), (0, 1, 0))

    def test_metadata_checks_alone_do_not_prove_no_source_events_were_omitted(self):
        # A lying adapter can declare a structurally valid but incomplete page.
        # No count-only report is allowed to be presented as source completeness.
        self.adapter.page_hook = lambda p: replace(p, events=())
        result = self.refresh()
        self.assertEqual((result.cursor, result.projection_count), (1, 0))
        self.assertEqual(self.adapter._heads['event_A'].status, 'active')
        self.assertFalse(hasattr(result, 'source_completeness_verified'))

    def test_read_final_epoch_change_rejects_and_evicts_cached_projection(self):
        self.refresh()
        self.adapter.validate_hook = lambda: self.adapter.deny(self.ctx.principal_id, 'private_A')
        self.rejects(self.read, 'projection_scan_changed')
        self.assertEqual(self.progress().projection_count, 0)

    def test_concurrent_refreshes_cannot_both_commit_from_same_base(self):
        barrier = Barrier(2)
        self.adapter.page_hook = lambda p: (barrier.wait(timeout=5), p)[1]
        def refresh():
            try:
                return self.refresh()
            except ProjectionError as error:
                return error.category
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: refresh(), range(2)))
        self.assertEqual(sum(r == 'projection_refresh_conflict' for r in results), 1)
        self.assertEqual(self.progress().projection_count, 1)

    def test_clear_during_final_validation_is_not_overwritten(self):
        self.adapter.validate_hook = lambda: self.index.clear(access=self.ctx)
        self.rejects(self.refresh, 'projection_refresh_conflict')
        self.assertEqual(self.progress().projection_count, 0)

    def test_invalid_access_and_event_ids_are_redacted(self):
        for access in (None, AccessScope('../invalid', frozenset({'private_A'})),
                       AccessScope('valid', {'private_A'})):
            self.rejects(lambda: self.index.refresh(self.adapter, access=access), 'projection_access_invalid')
        self.rejects(lambda: self.index.read(self.adapter, '../event_A', access=self.ctx), 'projection_source_invalid')

    def test_protocol_and_module_do_not_introduce_runtime_or_authority_writes(self):
        from backend import g3_projection_feed as module
        self.assertEqual({k for k in vars(PagedG3ReadOnlySourceAdapter) if not k.startswith('_')},
                         {'begin_scan', 'read_page', 'validate_scan'})
        source = inspect.getsource(module)
        for forbidden in ('sqlite3', 'requests', 'httpx', 'openai', 'os.environ', 'getenv(', 'create_session'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
