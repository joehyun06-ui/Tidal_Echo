"""GM-02A synthetic files only. No model, network, relay or production fixture."""
import concurrent.futures
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.session_lifecycle import Lifecycle, Denied, Target, Snapshot, SourceRef


class Interrupted(RuntimeError):
    pass


def stop_at(point):
    def inject(actual):
        if actual == point:
            raise Interrupted(point)
    return inject


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name in ('connect', 'connect_ex'):
            self.enterContext(patch.object(socket.socket, name, side_effect=AssertionError('network')))
        self.enterContext(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS')))
        self.path = Path(self.temp.name, 'fixture.sqlite')
        self.target = Target('conversation', 'alice', 'w0', 0)
        self.db = Lifecycle.create_fixture(self.path, self.target)
        self.data = Snapshot(('pending-1',), 'state', ('anchor',), ('derived',))
        self.complete()

    def complete(self, **changes):
        job = dict(provider='api', window='w0', message='m1', generation='api-gen-m1',
                   status='completed', reply='r1', upstream_unknown=False)
        reply = dict(window='w0', message='m1', generation='api-gen-m1', reply='r1',
                     provider='api', canonical=True)
        self.db.fixture_turn(job | changes, reply)

    def close(self, **kw):
        return self.db.closeout(self.target, 'c1', self.data, **kw)

    def ready(self):
        self.close()
        return self.db.reentry(self.target, 'c1')

    def commit(self, package, **kw):
        return self.db.safe_commit_point(self.target, 'c1', package['version'], **kw)

    def test_pointer_and_send_gate_across_restart(self):
        self.assertTrue(self.db.send_allowed(self.target))
        p = self.ready()
        self.db = Lifecycle(self.path)
        self.assertEqual(self.db.head(self.target)['window'], 'w0')
        self.assertFalse(self.db.send_allowed(self.target))
        self.assertFalse(self.db.send_allowed(Target('conversation', 'alice', p['candidate'], 1)))
        self.assertEqual(p['pending'], ['pending-1'])
        self.commit(p)
        self.assertTrue(self.db.send_allowed(Target('conversation', 'alice', p['candidate'], 1)))
        self.assertFalse(self.db.send_allowed(self.target))

    def test_unknown_boundary_and_fixed_retry(self):
        self.close()
        with self.assertRaises(Interrupted):
            self.db.reentry(self.target, 'c1', fault=stop_at('pre_boundary'))
        before = self.db.reconcile(self.target, 'c1', 'prepare')
        self.assertEqual(before['phase'], 'FAILED_PRE_BOUNDARY')
        with self.assertRaises(Interrupted):
            self.db.reentry(self.target, 'c1', fault=stop_at('dispatch'))
        after = self.db.reconcile(self.target, 'c1', 'prepare')
        self.assertEqual(after['candidate'], before['candidate'])
        self.assertEqual(after['phase'], 'UNKNOWN_POST_BOUNDARY')
        self.assertTrue(after['reconcile_only'])
        with self.assertRaises(Denied):
            self.db.reentry(self.target, 'c1')
        self.assertEqual(after, self.db.reconcile(self.target, 'c1', 'prepare'))

    def test_safe_commit_fault_matrix(self):
        p = self.ready()
        for guard in Lifecycle.COMMIT_CHECKS:
            with self.subTest(guard=guard):
                with self.assertRaises(Interrupted):
                    self.commit(p, fault=stop_at(guard))
                self.assertEqual(self.db.head(self.target), dict(window='w0', epoch=0))
                self.assertFalse(self.db.send_allowed(self.target))
                self.assertNotEqual(self.db.reconcile(self.target, 'c1', 'switch')['phase'], 'COMMITTED')

    def test_closeout_repeat_is_identical_and_payload_conflict(self):
        receipt = self.close()
        self.assertEqual(self.close(), receipt)
        with self.assertRaisesRegex(Denied, 'payload conflict'):
            self.db.closeout(self.target, 'c1', Snapshot((), 'different', (), ()))

    def test_closeout_crash_before_commit(self):
        with self.assertRaises(Interrupted):
            self.close(fault=stop_at('pre_boundary'))
        self.assertEqual(self.db.reconcile(self.target, 'c1', 'close')['crossed'], 0)
        self.assertEqual(self.close()['window_id'], 'w0')

    def test_closeout_response_loss_process_exit_and_readonly_recovery(self):
        # Terminate a separate interpreter immediately after the SQLite commit.
        script = """
import os, sys
from backend.session_lifecycle import *
db = Lifecycle(sys.argv[1])
db.closeout(Target('conversation','alice','w0',0), 'c1',
            Snapshot(('pending-1',),'state',('anchor',),('derived',)),
            fault=lambda point: os._exit(73) if point == 'response_loss' else None)
"""
        child = subprocess.run([sys.executable, '-c', script, str(self.path)],
                               cwd=Path(__file__).resolve().parents[2], check=False)
        self.assertEqual(child.returncode, 73)
        fresh = Lifecycle(self.path)
        state = fresh.reconcile(self.target, 'c1', 'close')
        self.assertEqual(state['phase'], 'COMMITTED')
        self.assertEqual(fresh.head(self.target)['epoch'], 0)
        self.assertFalse(fresh.send_allowed(self.target))
        self.assertEqual(fresh.reentry(self.target, 'c1')['pending'], ['pending-1'])

    def test_closeout_dispatch_unknown_blocks_every_new_request(self):
        with self.assertRaises(Interrupted):
            self.close(fault=stop_at('dispatch'))
        with self.assertRaisesRegex(Denied, 'reconcile only'):
            self.close()
        with self.assertRaises(Denied):
            self.db.closeout(self.target, 'other', self.data)
        self.assertFalse(self.db.send_allowed(self.target))
        self.assertEqual(self.db.reconcile(self.target, 'c1', 'close')['phase'], 'UNKNOWN_POST_BOUNDARY')

    def test_reentry_response_loss_same_candidate(self):
        self.close()
        with self.assertRaises(Interrupted):
            self.db.reentry(self.target, 'c1', fault=stop_at('response_loss'))
        r = self.db.reconcile(self.target, 'c1', 'prepare')
        self.assertEqual(self.db.reentry(self.target, 'c1')['candidate'], r['candidate'])
        self.assertIsNone(r['final_receipt'])

    def test_concurrent_reentry_has_one_candidate(self):
        self.close()
        barrier = threading.Barrier(2)
        def run(_):
            db = Lifecycle(self.path)
            barrier.wait(10)
            try:
                return db.reentry(self.target, 'c1')['candidate']
            except Denied as exc:
                self.assertEqual(str(exc), 'reconcile only')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            candidates = set(pool.map(run, range(2))) - {None}
        self.assertEqual(candidates, {self.db.reconcile(self.target, 'c1', 'prepare')['candidate']})

    def test_concurrent_safe_commit_lost_winner_response(self):
        p = self.ready()
        barrier = threading.Barrier(2)
        def run(_):
            db = Lifecycle(self.path)
            barrier.wait(10)
            try:
                return db.safe_commit_point(self.target, 'c1', p['version'], fault=stop_at('response_loss'))
            except Interrupted:
                return 'winner lost response'
            except Denied as exc:
                self.assertEqual(str(exc), 'reconcile only')
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(results.count('winner lost response'), 1)
        final = self.db.reconcile(self.target, 'c1', 'switch')['final_receipt']
        self.assertTrue(all(v in (None, 'winner lost response', final) for v in results))
        self.assertEqual(self.commit(p), final)
        self.assertEqual(self.db.head(self.target), dict(window=p['candidate'], epoch=1))

    def test_unknown_switch_never_promotes(self):
        p = self.ready()
        with self.assertRaises(Interrupted):
            self.commit(p, fault=stop_at('dispatch'))
        for _ in range(2):
            with self.assertRaisesRegex(Denied, 'reconcile only'):
                self.commit(p)
            self.assertIsNone(self.db.reconcile(self.target, 'c1', 'switch')['final_receipt'])
            self.assertEqual(self.db.head(self.target)['epoch'], 0)

    def test_stale_owner_epoch_and_window(self):
        for target in (Target('other', 'alice', 'w0', 0), Target('conversation','bob','w0',0),
                       Target('conversation','alice','w0',1), Target('conversation','alice','else',0)):
            with self.subTest(target=target), self.assertRaises(Denied):
                self.db.closeout(target, 'c1', self.data)
        self.assertEqual(self.close()['conversation_id'], 'conversation')

    def test_revoked_conversation_rejects_switch_and_readback(self):
        p = self.ready()
        self.db.fixture_owner_revoked()
        with self.assertRaises(Denied):
            self.commit(p)
        with self.assertRaises(Denied):
            self.db.reconcile(self.target, 'c1', 'close')

    def test_streaming_or_tool_busy_only_pending(self):
        for tool, streaming, unresolved in [('running',False,False), ('settled', True,False),
                                             ('settled',False,True)]:
            with self.subTest(tool=tool, streaming=streaming, unresolved=unresolved):
                with self.db._tx(write=True) as db:
                    db.execute('UPDATE turn SET tool=?,streaming=?,unresolved=?', (tool,streaming,unresolved))
                self.db.request_continuation(self.target)
                self.assertTrue(self.db.continuation_pending(self.target))
                with self.assertRaises(Denied):
                    self.close()
                self.assertEqual(self.db.head(self.target)['epoch'], 0)

    def test_turn_changes_after_prepare(self):
        p = self.ready()
        self.complete(message='m2', generation='api-gen-m2')
        with self.assertRaises(Denied):
            self.commit(p)
        self.assertFalse(self.db.send_allowed(self.target))

    def test_api_uncertain_is_not_success(self):
        for change in [dict(upstream_unknown=True), dict(status='running'), dict(reply='wrong'),
                       dict(message='stale'), dict(generation='other')]:
            self.complete(**change)
            with self.subTest(change=change), self.assertRaisesRegex(Denied,'unknown'):
                self.close()

    def test_codex_requires_native_and_canonical_bindings(self):
        job = dict(window='w0', message='m', reply='r', generation='n', provider='codex',
                   client='client', callback='callback', status='completed', native=True)
        reply = dict(job, canonical=True)
        self.db.fixture_turn(dict(job, native=False), reply)
        with self.assertRaisesRegex(Denied,'unsupported'):
            self.close()
        for field in ['client','callback','generation','window','message','reply','canonical']:
            self.db.fixture_turn(job, dict(reply, **{field: None}))
            with self.subTest(field=field), self.assertRaisesRegex(Denied,'unknown'):
                self.close()
        self.db.fixture_turn(job, reply)
        self.assertEqual(self.close()['completion_state'], 'completed')

    def sources(self):
        refs = [SourceRef(i, f'event-{i}', 1, '0'*64) for i in (1,2,3)]
        for ref in refs:
            self.db.fixture_source(ref, owner='alice')
        return refs

    def delta(self, page):
        return self.db.reentry_delta(self.target, 'c1', page)

    def test_delta_duplicate_overlap_retry_and_monotonic_cursor(self):
        self.ready()
        a,b,c = self.sources()
        p = self.delta([a,b])
        self.assertEqual(p, self.delta([a,b]))
        p = self.delta([b,c])
        self.assertEqual(p['source_cursor'], 3)
        self.assertEqual(len(p['source_refs']), 3)
        receipt = self.commit(p)
        t = Target('conversation', 'alice', receipt['window'], 1)
        self.assertEqual(self.db.head(t)['epoch'], 1)
        self.assertEqual(self.db.head(t)['window'], p['candidate'])

    def test_out_of_order_gap_and_partial_page_roll_back(self):
        p = self.ready()
        a,b,c = self.sources()
        for page in ([a,c,b], [b], [a,b,a]):
            with self.subTest(page=page), self.assertRaises(Denied):
                self.delta(page)
            self.assertEqual(self.db.reentry(self.target,'c1'), p)
        def broken_page():
            yield a
            raise Interrupted()
        with self.assertRaises(Interrupted):
            self.delta(broken_page())
        self.assertEqual(self.db.reentry(self.target,'c1'), p)

    def test_unseen_tail_blocks_switch(self):
        self.ready()
        a,b,_ = self.sources()
        with self.assertRaises(Denied):
            self.commit(self.delta([a,b]))

    def test_ready_reference_is_revalidated_for_acl_revocation_and_revision(self):
        self.ready()
        refs = self.sources()
        p = self.delta(refs)
        for ref, owner, revoked in [(refs[0],'alice',True), (refs[0],'bob',False),
                                   (SourceRef(1,'event-1',2,'0'*64),'alice',False)]:
            self.db.fixture_source(ref,owner=owner,revoked=revoked)
            with self.subTest(owner=owner,ref=ref), self.assertRaises(Denied):
                self.commit(p)
            with self.assertRaises(Denied):
                self.db.reentry(self.target,'c1')
            self.assertEqual(self.db.head(self.target)['window'], 'w0')

    def test_candidate_mismatch_even_with_valid_package_checksum(self):
        import json
        from backend.session_lifecycle import encode, digest
        p = self.ready()
        with self.db._tx(write=True) as db:
            body = json.loads(db.execute('SELECT body FROM package').fetchone()[0])
            body['candidate'] = 'different'
            db.execute('UPDATE package SET body=?,version=?',(encode(body),digest(body)))
        with self.assertRaisesRegex(Denied,'candidate'):
            self.db.safe_commit_point(self.target,'c1',digest(body))
        self.assertEqual(self.db.head(self.target)['window'], 'w0')
        self.assertFalse(self.db.send_allowed(Target('conversation','alice',p['candidate'],1)))

    def test_corrupt_or_missing_fixture_never_guesses_history(self):
        self.ready()
        self.path.write_bytes(b'broken')
        with self.assertRaises(sqlite3.DatabaseError):
            Lifecycle(self.path)
        self.path.unlink()
        with self.assertRaises(sqlite3.DatabaseError):
            Lifecycle(self.path)
        self.assertFalse(self.path.exists())

    def test_database_rejects_other_final_writers(self):
        p = self.ready()
        with self.assertRaises(sqlite3.DatabaseError), self.db._tx(write=True) as db:
            db.execute('INSERT INTO final VALUES(1,?,?,?,?)', ('c1',p['candidate'],p['version'],'now'))
        self.assertEqual(self.db.head(self.target)['window'], 'w0')

    def test_history_and_reopen_never_promote_or_resend(self):
        p = self.ready()
        receipt = self.commit(p)
        for _ in range(2):
            fresh = Lifecycle(self.path)
            self.assertEqual(fresh.reconcile(self.target,'c1','switch')['final_receipt'], receipt)
            self.assertEqual(fresh.head(self.target)['epoch'], 1)
            self.assertEqual(fresh.closeout(self.target,'c1',self.data)['conversation_id'], 'conversation')
            with self.assertRaises(Denied):
                fresh.closeout(self.target,'new-close',self.data)

    def test_cursor_advances_after_ready_then_catch_up(self):
        p = self.ready()
        refs = self.sources()
        with self.assertRaisesRegex(Denied, 'cursor'):
            self.commit(p)
        updated = self.delta(refs)
        self.assertNotEqual(updated['version'], p['version'])
        with self.assertRaises(Denied):
            self.commit(p)
        self.assertEqual(self.commit(updated)['epoch'], 1)

    def test_acl_change_after_dispatch_before_switch_transaction(self):
        self.ready()
        refs = self.sources()
        p = self.delta(refs)
        def withdraw(point):
            if point == 'dispatch':
                Lifecycle(self.path).fixture_source(refs[0], owner='other')
        with self.assertRaisesRegex(Denied, 'invalid source'):
            self.commit(p, fault=withdraw)
        self.assertFalse(self.db.send_allowed(self.target))
        self.assertIsNone(self.db.reconcile(self.target, 'c1', 'switch')['final_receipt'])

    def test_second_switch_preserves_conversation_watermark_and_old_terminal_windows(self):
        self.ready()
        refs = self.sources()
        first = self.commit(self.delta(refs))
        new = Target('conversation', 'alice', first['window'], 1)
        job = dict(window=new.window, message='2', reply='r', generation='api-gen-2',
                   status='completed', provider='api', upstream_unknown=False)
        self.db.fixture_turn(job, dict(job, canonical=True))
        close = self.db.closeout(new, 'c2', self.data)
        self.assertEqual(close['source_cursor'], 3)
        p = self.db.reentry(new, 'c2')
        second = self.db.safe_commit_point(new, 'c2', p['version'])
        self.assertEqual(second['epoch'], 2)
        self.assertFalse(self.db.send_allowed(new))
        self.assertFalse(self.db.send_allowed(self.target))
        self.assertTrue(self.db.send_allowed(Target('conversation','alice',second['window'],2)))
        self.assertEqual(Lifecycle(self.path).head(new)['window'], second['window'])

    def test_semantically_corrupt_receipt_rejects_reopen(self):
        self.commit(self.ready())
        with self.db._tx(write=True) as db:
            db.execute("DELETE FROM operation WHERE kind='switch'")
        with self.assertRaisesRegex(Denied, 'receipt disagreement'):
            Lifecycle(self.path)

    def test_unseen_package_and_unknown_completion_deny_send(self):
        self.complete(upstream_unknown=True)
        self.assertFalse(self.db.send_allowed(self.target))
        self.assertFalse(self.db.send_allowed(Target('conversation','alice','uncreated',1)))
        with self.assertRaisesRegex(Denied, 'unknown'):
            self.close()
