"""Real stdio/reader/ledger integration; the child is synthetic, never Codex.

Both processes forbid networking. No SDK, login, model request or production
database is used. RPC names prefixed fixture/ exist only in the disposable child.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.codex_app_server_shared_transport import (
    CodexSharedAppServerRuntime, CodexSharedTransportConfig, CodexTransportError,
)
from backend.codex_generation_streaming import StreamingCodexGenerationRuntime
from backend.usage_ledger import build_view
from backend.usage_ledger_codex import NOTIFICATIONS
from backend.tests import test_usage_ledger_codex as fixtures


CHILD = r'''
import json
import socket
import sys

def offline(*args, **kwargs):
    raise AssertionError('offline synthetic child')
socket.socket.connect = offline
socket.create_connection = socket.getaddrinfo = offline

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialized':
        continue
    if method == 'fixture/eof':
        break
    if method == 'fixture/malformed':
        print('{"method":"turn/started","method":"turn/completed"}', flush=True)
        continue
    if method == 'fixture/emit':
        for event in message['params']['events']:
            emit(event)
    emit({'id': message['id'], 'result': {}})
'''


def event(method, n, *, thread='thread-a', **params):
    body = {'threadId': thread}
    if method in {'turn/started', 'turn/completed'}:
        body['turn'] = {'id': f'turn-{n}', 'status': 'completed' if method.endswith('completed') else 'inProgress'}
    else:
        body['turnId'] = f'turn-{n}'
    return {'method': method, 'params': {**body, **params}}


def turn(n, raw=None, *, thread='thread-a'):
    return [event('turn/started', n, thread=thread),
            event('thread/tokenUsage/updated', n, thread=thread, tokenUsage=raw or fixtures.usage(n*100, n*20, n*5)),
            event('turn/completed', n, thread=thread)]


class NativeTransportLedgerTests(unittest.IsolatedAsyncioTestCase):
    make = fixtures.CodexLedgerTests.make
    job = fixtures.CodexLedgerTests.job

    def setUp(self):
        fixtures.CodexLedgerTests.setUp(self)
        self.root = Path(self.relay).parent
        self.child = self.root/'synthetic-app-server.py'
        self.child.write_text(CHILD, encoding='utf-8')
        self.launches = 0
        self.consumer_fails = False

    async def asyncSetUp(self):
        self.runtime, self.scope = self.runtime_pair()

    def runtime_pair(self):
        home = self.root/'synthetic-home'; home.mkdir(exist_ok=True)
        async def launcher(*command, **kwargs):
            self.assertEqual(command[0], 'synthetic-codex-not-an-executable')
            self.assertEqual(kwargs['env']['CODEX_HOME'], str(home))
            self.launches += 1
            return await asyncio.create_subprocess_exec(
                sys.executable, str(self.child), stdin=kwargs['stdin'],
                stdout=kwargs['stdout'], stderr=kwargs['stderr'], cwd=kwargs['cwd'],
                env=kwargs['env'], limit=kwargs['limit'])
        runtime = CodexSharedAppServerRuntime(
            CodexSharedTransportConfig(True, home, self.root, 3),
            _runtime_resolver=lambda: 'synthetic-codex-not-an-executable',
            _process_launcher=launcher,
            _parent_environment={'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8'})
        self.addAsyncCleanup(runtime.close)
        def other_consumer(method, params):
            if self.consumer_fails:
                raise OSError('synthetic private consumer error')
        runtime.scope(notifications=NOTIFICATIONS, methods=frozenset(), handler=other_consumer)
        self.sink = SimpleNamespace(usage_collector=self.collector,
                                    foundation=SimpleNamespace(runtime=runtime))
        runtime.scope(notifications=NOTIFICATIONS, methods=frozenset(),
                      handler=lambda m, p: StreamingCodexGenerationRuntime._on_ledger_usage(self.sink, m, p))
        scope = runtime.scope(methods=frozenset({'fixture/emit', 'fixture/eof', 'fixture/malformed'}))
        return runtime, scope

    async def emit(self, events):
        # The child ACK is last: completion proves the reader dispatched each line.
        await self.scope.request('fixture/emit', {'events': events})

    async def flush(self):
        await asyncio.to_thread(self.collector.reconcile, epoch=self.runtime.connection_epoch)

    async def whole(self, n, raw=None, *, sid='session-a', thread='thread-a'):
        self.job(n, sid=sid, thread=thread)
        await self.emit(turn(n, raw, thread=thread)); await self.flush()
        return self.ledger.records('codex', sid)[-1]

    async def test_wire_events_wait_for_exact_durable_ack_identity(self):
        await self.emit(turn(1)); await self.flush()
        self.assertEqual(self.ledger.records('codex'), [])
        self.job(1, turn='not-the-observed-turn'); await self.flush()
        self.assertEqual(self.ledger.records('codex'), [])
        with sqlite3.connect(self.native) as conn:
            conn.execute("UPDATE codex_generation_jobs SET turn_id='turn-1'")
        await self.flush()
        row = self.ledger.records('codex')[0]
        self.assertIsNone(row['usage_baseline']); self.assertEqual(row['raw_usage']['total']['inputTokens'], 100)
        self.assertEqual(self.launches, 1)

    async def test_wire_duplicate_multirequest_observations_use_one_terminal_delta(self):
        await self.whole(1)
        self.job(2)
        middle, final = fixtures.usage(300, 60, 15), fixtures.usage(450, 90, 20)
        events = [event('turn/started', 2)]
        events += [event('thread/tokenUsage/updated', 2, tokenUsage=raw) for raw in (middle, middle, final, final)]
        events += [event('turn/completed', 2)]*2
        await self.emit(events); await self.flush(); await self.flush()
        rows = self.ledger.records('codex'); self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['normalized']['metrics']['input'], 350)
        self.assertTrue(rows[-1]['usage_complete']); self.assertIsNone(rows[-1]['cost']['amount'])
        self.assertIsNone(rows[-1]['normalized']['metrics']['cache_read'])

    async def test_eof_reconnect_breaks_baseline_without_replaying_or_relaunching_a_turn(self):
        await self.whole(1); epoch = self.runtime.connection_epoch
        with self.assertRaisesRegex(CodexTransportError, 'codex_app_server_unavailable'):
            await self.scope.request('fixture/eof', {})
        self.assertGreater(self.runtime.connection_epoch, epoch)
        self.assertIsNone(self.runtime._process); self.assertEqual(self.launches, 1)
        await self.flush()
        self.assertIsNone((await self.whole(2))['usage_baseline'])
        self.assertEqual(self.launches, 2)
        self.assertEqual((await self.whole(3))['normalized']['metrics']['input'], 100)
        self.assertTrue(build_view(self.ledger, 'codex', 'session-a')['collection_health']['has_observed_gaps'])

    async def test_malformed_jsonl_terminates_owner_and_cannot_bridge_epochs(self):
        await self.whole(1); before = copy.deepcopy(self.ledger.records('codex'))
        with self.assertRaisesRegex(CodexTransportError, 'codex_app_server_protocol_error'):
            await self.scope.request('fixture/malformed', {})
        self.assertIsNone(self.runtime._process); await self.flush()
        self.assertEqual(self.ledger.records('codex'), before)
        self.assertIsNone((await self.whole(2))['usage_baseline'])
        self.assertEqual((await self.whole(3))['normalized']['metrics']['input'], 100)

    async def test_fresh_observer_replay_is_idempotent_and_not_a_baseline(self):
        await self.whole(1); await self.runtime.close()
        self.collector = self.make()
        self.runtime, self.scope = self.runtime_pair()
        await self.emit(turn(1)); await self.flush()
        self.assertEqual(len(self.ledger.records('codex')), 1)
        self.assertEqual(self.collector.health()['captured'], 0)
        self.assertIsNone((await self.whole(2))['usage_baseline'])
        self.assertEqual((await self.whole(3))['normalized']['metrics']['input'], 100)

    async def test_durable_turn_without_notifications_breaks_wire_counter_continuity(self):
        await self.whole(1); self.job(2, status='completed')
        self.assertIsNone((await self.whole(3))['usage_baseline'])
        self.assertEqual((await self.whole(4))['normalized']['metrics']['input'], 100)

    async def test_changed_late_usage_invalidates_a_successor_that_already_started(self):
        await self.whole(1); before = copy.deepcopy(self.ledger.records('codex'))
        self.job(2); await self.emit([event('turn/started', 2)])
        await self.emit([event('thread/tokenUsage/updated', 1, tokenUsage=fixtures.usage(150, 30, 7)),
                         event('thread/tokenUsage/updated', 2, tokenUsage=fixtures.usage(300, 60, 15)),
                         event('turn/completed', 2)])
        await self.flush(); rows = self.ledger.records('codex')
        self.assertEqual(rows[0], before[0]); self.assertIsNone(rows[-1]['usage_baseline'])
        self.assertFalse(rows[-1]['usage_complete'])
        self.assertIsNone((await self.whole(3))['usage_baseline'])
        self.assertEqual((await self.whole(4))['normalized']['metrics']['input'], 100)
        self.assertEqual(build_view(self.ledger, 'codex', 'session-a')['collection_health']['issues']['late_event'], 1)

    async def test_invalid_late_payload_invalidates_successor_without_retaining_private_fields(self):
        await self.whole(1); self.job(2)
        await self.emit([event('turn/started', 2),
                         event('thread/tokenUsage/updated', 1, tokenUsage={'authorization': 'synthetic-private-token'}),
                         event('thread/tokenUsage/updated', 2, tokenUsage=fixtures.usage(300, 60, 15)),
                         event('turn/completed', 2)])
        await self.flush(); row = self.ledger.records('codex')[-1]
        self.assertIsNone(row['usage_baseline']); self.assertFalse(row['usage_complete'])
        view = build_view(self.ledger, 'codex', 'session-a')
        self.assertEqual(view['collection_health']['issues']['invalid_usage'], 1)
        self.assertNotIn('synthetic-private-token', json.dumps(view))
        self.assertNotIn('synthetic-private-token', json.dumps(self.ledger.records('codex')))

    async def test_invalid_usage_during_disk_reconciliation_invalidates_the_staged_revision(self):
        await self.whole(1); self.job(2); await self.emit(turn(2))
        entered, release = threading.Event(), threading.Event()
        binding = self.collector._binding
        def paused_binding(state):
            result = binding(state); entered.set()
            if not release.wait(3): raise AssertionError('synthetic synchronization timeout')
            return result
        with patch.object(self.collector, '_binding', side_effect=paused_binding):
            task = asyncio.create_task(self.flush())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                await self.emit([event('thread/tokenUsage/updated', 2, tokenUsage={'authorization': 'synthetic-private-token'})])
            finally:
                release.set(); await task
        self.assertEqual(len(self.ledger.records('codex')), 1)
        await self.flush(); row = self.ledger.records('codex')[-1]
        self.assertIsNone(row['usage_baseline']); self.assertFalse(row['usage_complete'])

    async def test_changed_old_counter_does_not_invalidate_another_threads_delta(self):
        await self.whole(1)
        await self.whole(2, fixtures.usage(), sid='session-b', thread='thread-b')
        self.job(3); self.job(4, sid='session-b', thread='thread-b')
        await self.emit([event('turn/started', 3), event('turn/started', 4, thread='thread-b'),
                         event('thread/tokenUsage/updated', 1, tokenUsage=fixtures.usage(150, 30, 7)),
                         event('thread/tokenUsage/updated', 3, tokenUsage=fixtures.usage(300, 60, 15)),
                         event('turn/completed', 3),
                         event('thread/tokenUsage/updated', 4, thread='thread-b', tokenUsage=fixtures.usage(200, 40, 10)),
                         event('turn/completed', 4, thread='thread-b')])
        await self.flush()
        self.assertIsNone(self.ledger.records('codex', 'session-a')[-1]['usage_baseline'])
        self.assertEqual(self.ledger.records('codex', 'session-b')[-1]['normalized']['metrics']['input'], 100)

    async def test_an_unrelated_failing_consumer_cannot_drop_ledger_notifications(self):
        self.consumer_fails = True
        await self.whole(1)
        row = await self.whole(2)
        self.assertEqual(row['normalized']['metrics']['input'], 100)
        self.assertTrue(row['usage_complete']); self.assertEqual(self.launches, 1)
        self.assertNotIn('private consumer error', json.dumps(self.collector.health()))


if __name__ == '__main__': unittest.main()
