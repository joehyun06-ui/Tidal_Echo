"""Real ledger/service composition over disposable files; no live requests or SDK."""
import asyncio
import copy
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend import codex_generation_store as native_store
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegration
from backend.usage_ledger import LedgerError, build_view
from backend.usage_ledger_capture import RouteProfile
from backend.usage_ledger_composition import LedgerServices
from backend.web_session_provider_authority import WebSessionProviderAuthority

ROOT = Path(__file__).resolve().parents[2]
KEY = b'synthetic-continuation-only-key-12345'
PREFIX_KEY = b'synthetic-prefix-only-key-12345678'
ROUTE = {'url': 'https://synthetic.invalid/v1', 'model': 'synthetic-model'}
RAW = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
       'prompt_tokens_details': {'cached_tokens': 80}}
PRICE = {'currency': 'USD', 'price_id': 'synthetic-v1', 'write_in_input_rate': True,
         'per_million': {'input': '1', 'output': '2', 'cache_read': '0.1'}}


def profile(model='synthetic-model', revision='v1', price=None):
    return RouteProfile(ROUTE['url'], model, 'chat', 'synthetic-api', 'openai_chat', revision,
                        price=copy.deepcopy(PRICE if price is None else price))


def usage(n):
    total = {'inputTokens': n*100, 'outputTokens': n*20, 'totalTokens': n*120,
             'reasoningOutputTokens': n*5}
    return {'total': total, 'last': total, 'modelContextWindow': 200000}


class CompositionFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.relay = self.root/'relay.sqlite'; self.native = self.root/'native.sqlite'
        self.path = self.root/'ledger.sqlite'; self.lock = RLock()
        self.cfg = {'sessions': [{'id': s, 'title': 'synthetic', 'provider': m, 'since_id': 0, 'created_at': ''}
                                for s, m in [('a', 'api'), ('b', 'codex')]]}
        self.legacy = SimpleNamespace(RELAY_DB=self.relay, load_config=lambda: copy.deepcopy(self.cfg),
                                     save_config=lambda c: self.cfg.update(copy.deepcopy(c)),
                                     now_iso=lambda: datetime.now(timezone.utc).isoformat())
        self.authority = WebSessionProviderAuthority(self.legacy)
        with sqlite3.connect(self.relay) as c:
            c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
        native_store.initialize(self.native)
        native_store.pin_session(self.native, api_session='b', model='synthetic-model', model_provider='openai',
                                 reasoning_effort=None, persona_hash='a'*64)
        with closing(native_store.connect(self.native)) as c:
            c.execute("UPDATE codex_sessions SET thread_id='synthetic-thread',thread_attempt_id='mock',cwd=? WHERE api_session='b'", (str(self.root),))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline-only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline-only')))

    def bundle(self, *, capture=False, bind=True, **kwargs):
        options = {'ledger_path': self.path, 'relay_db': self.relay, 'native_db': self.native,
                   'continuation_key': KEY}
        if capture:
            options.update(api_profiles=[profile()], fingerprint_key=PREFIX_KEY,
                           codex_provider_id='synthetic-subscription', codex_cli_version='0.160.0', exclusive_threads=True)
        options.update(kwargs)
        services = LedgerServices(**options)
        if bind: services.bind_authority(self.authority, self.lock)
        return services

    def seed(self, n, *, model=None):
        with sqlite3.connect(self.relay) as c:
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                      (n, datetime.now(timezone.utc).isoformat(), 'in', 'user', 'synthetic private text', json.dumps({'api_session': 'b'})))
        job = native_store.enqueue_job(self.native, api_session='b', canonical_message_id=n, input_digest='a'*64,
                                       generation_id=f'synthetic-gen-{n}', client_message_id=f'synthetic-client-{n}', callback_identity=f'synthetic-callback-{n}')
        with closing(native_store.connect(self.native)) as c:
            c.execute("UPDATE codex_generation_jobs SET status='completed',thread_id='synthetic-thread',turn_id=? WHERE id=?", (f'turn-{n}', job['id']))
            if model: c.execute("UPDATE codex_sessions SET model=? WHERE api_session='b'", (model,))

    def emit(self, services, n, *, epoch=1):
        collector = services.codex
        for method, params in [('turn/started', {'turn': {'id': f'turn-{n}'}}),
                               ('thread/tokenUsage/updated', {'turnId': f'turn-{n}', 'tokenUsage': usage(n)}),
                               ('turn/completed', {'turn': {'id': f'turn-{n}', 'status': 'completed'}})]:
            collector.receive(method, {'threadId': 'synthetic-thread', **params}, epoch=epoch)
        collector.reconcile(epoch=epoch)


class CompositionTests(CompositionFixture):
    def test_reader_only_defaults_create_no_collectors_or_observation_runs(self):
        services = self.bundle()
        self.assertEqual(services.api_runtime_kwargs(), {})
        self.assertEqual(services.codex_runtime_kwargs(), {})
        with closing(services.ledger.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM usage_ledger_runs').fetchone()[0], 0)
        self.assertIsNone(build_view(services.ledger, 'api', 'a')['summary']['metrics']['input']['value'])

    def test_runtime_kwargs_share_exact_services_and_never_mutate_runtime(self):
        services = self.bundle(capture=True)
        self.assertIs(services.api_runtime_kwargs()['usage_collector'], services.api)
        self.assertIs(services.codex_runtime_kwargs()['usage_collector'], services.codex)
        self.assertIs(services.codex_runtime_kwargs()['compaction_collector'], services.manual_compactions)
        self.assertIs(services.deletion_kwargs()['usage_ledger'], services.ledger)

    def test_unbound_sources_are_unavailable_and_authority_can_bind_only_once(self):
        services = self.bundle(bind=False)
        self.assertIsNone(services._row('a'))
        for op in (services.action_kwargs, services.deletion_kwargs):
            with self.assertRaisesRegex(LedgerError, 'unbound'): op()
        services.bind_authority(self.authority, self.lock)
        with self.assertRaisesRegex(LedgerError, 'already_bound'): services.bind_authority(self.authority, self.lock)
        with self.assertRaisesRegex(LedgerError, 'factory_unavailable'): services.action_kwargs()

    def test_wrong_authority_source_is_rejected_without_binding(self):
        services = self.bundle(bind=False)
        authority = SimpleNamespace(row_for_session=lambda sid: {'provider': 'api'},
                                    legacy=SimpleNamespace(RELAY_DB=self.root/'other.sqlite'))
        with self.assertRaisesRegex(LedgerError, 'composition_invalid'): services.bind_authority(authority, self.lock)
        self.assertIsNone(services._row('a'))

    def test_receipt_initialization_failure_does_not_half_bind_or_leak_error(self):
        services = self.bundle(bind=False)
        with patch('backend.usage_ledger_composition.ContinuationCreation', side_effect=sqlite3.OperationalError('synthetic-private-storage-detail')):
            with self.assertRaisesRegex(LedgerError, '^ledger_composition_unavailable$'):
                services.bind_authority(self.authority, self.lock, create_session=AsyncMock())
        self.assertIsNone(services._row('a'))
        services.bind_authority(self.authority, self.lock)
        self.assertEqual(services._row('a')['provider'], 'api')

    async def test_real_factory_binding_registers_without_sending_or_starting_models(self):
        services = self.bundle(bind=False)
        integration = object.__new__(CodexCanaryLoopIntegration)
        integration.session_authority = self.authority; integration._session_lock = self.lock
        integration.runtime = SimpleNamespace(generation_enabled=True, controller=SimpleNamespace(pin_session=AsyncMock()))
        services.bind_authority(self.authority, self.lock, create_session=integration.create_web_session)
        creator = services.action_kwargs()['continuation_creator']
        result = await creator.create('a', {'request_id': '11111111-1111-4111-8111-111111111111',
                                          'source_version_id': 'a', 'text': 'synthetic continuation draft'})
        self.assertTrue(result['registered'])
        with sqlite3.connect(self.relay) as c: self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        integration.runtime.controller.pin_session.assert_not_awaited()

    async def test_independent_reader_observes_durable_start_and_actual_terminal(self):
        producer = self.bundle(capture=True); reader = self.bundle()
        attempt = producer.api.begin(ROUTE, 'chat', 'a', 'synthetic-turn')
        view = build_view(reader.ledger, 'api', 'a')
        self.assertEqual(view['collection_health']['unconfirmed'], 1)
        self.assertIsNone(view['summary']['metrics']['input']['value'])
        attempt.usage(RAW); await attempt.finish(None)
        view = build_view(reader.ledger, 'api', 'a')
        self.assertEqual(view['collection_health']['unconfirmed'], 0)
        self.assertEqual(view['summary']['metrics']['input']['value'], 100)
        self.assertEqual(build_view(reader.ledger, 'codex', 'b')['summary']['requests'], 0)

    async def test_restart_keeps_old_api_pending_instead_of_zero_or_failure(self):
        old = self.bundle(capture=True)
        old.api.begin(ROUTE, 'chat', 'a', 'unfinished-turn')
        restarted = self.bundle(capture=True)
        attempt = restarted.api.begin(ROUTE, 'chat', 'a', 'new-turn')
        attempt.usage(RAW); await attempt.finish(None)
        view = build_view(self.bundle().ledger, 'api', 'a')
        self.assertEqual(view['collection_health']['unconfirmed'], 1)
        self.assertEqual(view['summary']['requests'], 1)
        self.assertTrue(view['collection_health']['has_observed_gaps'])

    async def test_configuration_replacement_preserves_inflight_price_and_history(self):
        old = self.bundle(capture=True)
        attempt = old.api.begin(ROUTE, 'chat', 'a', 'old-config')
        new_price = copy.deepcopy(PRICE); new_price['price_id'] = 'synthetic-v2'; new_price['per_million']['input'] = '3'
        profiles = [profile('next-model', 'v2', new_price)]
        new = self.bundle(capture=True, api_profiles=profiles)
        # Mutating the caller's price object cannot alter either captured snapshot.
        profiles[0].price['per_million']['input'] = '99'
        attempt.usage(RAW); await attempt.finish(None)
        next_attempt = new.api.begin({**ROUTE, 'model': 'next-model'}, 'chat', 'a', 'new-config')
        next_attempt.usage(RAW); await next_attempt.finish(None)
        rows = self.bundle().ledger.records('api', 'a')
        self.assertEqual([r['price']['per_million']['input'] for r in rows], ['1', '3'])
        self.assertEqual([r['config_revision'] for r in rows], ['v1', 'v2'])
        self.assertEqual(rows[0]['model'], 'synthetic-model'); self.assertEqual(rows[1]['model'], 'next-model')

    def test_unconfigured_model_change_leaves_persistent_gap(self):
        services = self.bundle(capture=True)
        self.assertIsNone(services.api.begin({**ROUTE, 'model': 'not-configured'}, 'chat', 'a', 'turn'))
        view = build_view(self.bundle().ledger, 'api', 'a')
        self.assertEqual(view['collection_health']['issues']['route_unmatched'], 1)
        self.assertIsNone(view['summary']['metrics']['input']['value'])

    def test_native_restart_and_replay_do_not_create_a_fresh_baseline(self):
        old = self.bundle(capture=True)
        for n in (1, 2): self.seed(n); self.emit(old, n)
        self.assertEqual(old.ledger.records('codex')[-1]['normalized']['metrics']['input'], 100)
        restarted = self.bundle(capture=True)
        self.emit(restarted, 2)
        self.seed(3); self.emit(restarted, 3)
        rows = self.bundle().ledger.records('codex')
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[-1]['normalized']['metrics']['input'])
        self.seed(4); self.emit(restarted, 4)
        self.assertEqual(restarted.ledger.records('codex')[-1]['normalized']['metrics']['input'], 100)

    def test_native_missing_durable_turn_blocks_cross_gap_difference(self):
        services = self.bundle(capture=True)
        self.seed(1); self.emit(services, 1)
        self.seed(2)  # Durable work happened, but no notifications were observed.
        self.seed(3); self.emit(services, 3)
        row = self.bundle().ledger.records('codex')[-1]
        self.assertIsNone(row['usage_baseline']); self.assertFalse(row['usage_complete'])
        self.assertEqual(row['raw_usage']['total']['inputTokens'], 300)

    def test_epoch_or_model_change_blocks_native_cross_boundary_accounting(self):
        services = self.bundle(capture=True)
        self.seed(1); self.emit(services, 1)
        self.seed(2); self.emit(services, 2, epoch=2)
        self.assertIsNone(services.ledger.records('codex')[-1]['usage_baseline'])
        self.seed(3, model='another-model'); self.emit(services, 3, epoch=2)
        self.assertIsNone(services.ledger.records('codex')[-1]['usage_baseline'])

    def test_shared_thread_declaration_retains_raw_but_no_turn_delta(self):
        services = self.bundle(capture=True, exclusive_threads=False)
        for n in (1, 2): self.seed(n); self.emit(services, n)
        row = services.ledger.records('codex')[-1]
        self.assertIsNone(row['usage_baseline']); self.assertIsNone(row['cost']['amount'])
        self.assertEqual(row['raw_usage']['total']['inputTokens'], 200)

    async def test_real_session_delete_blocks_old_producer_late_finish(self):
        producer = self.bundle(capture=True); reader = self.bundle()
        attempt = producer.api.begin(ROUTE, 'chat', 'a', 'late-turn')
        result = reader.delete_session('a')
        self.assertTrue(result['deleted']['ledger_deleted'])
        attempt.usage(RAW); await attempt.finish(None)
        self.assertEqual(producer.ledger.records('api', 'a'), [])
        self.assertIsNone(self.authority.row_for_session('a'))

    async def test_version_delete_uses_same_ledger_retirement_hook(self):
        from backend import web_message_versions as versions
        services = self.bundle(capture=True)
        self.authority.publish_row(self.authority.new_row(provider='api', session_id='a-alt'), activate=False)
        with closing(versions.connect(self.relay)) as c, c:
            c.execute("INSERT INTO web_message_versions VALUES ('a-alt','a','a','regenerate',1,1,1)")
        attempts = [services.api.begin(ROUTE, 'chat', sid, 'version-turn') for sid in ('a', 'a-alt')]
        with self.lock:
            versions.delete_versions(self.authority, 'a', relay_db=self.relay, codex_store=self.native,
                                     **services.deletion_kwargs())
        for attempt in attempts: attempt.usage(RAW); await attempt.finish(None)
        self.assertEqual(services.ledger.records('api'), [])
        self.assertIsNone(self.authority.row_for_session('a-alt'))

    def test_invalid_config_is_rejected_before_new_ledger_is_created(self):
        bad_options = [{'api_profiles': []}, {'api_profiles': [profile()], 'fingerprint_key': b'short'},
                       {'api_profiles': [profile(), profile()], 'fingerprint_key': PREFIX_KEY},
                       {'codex_provider_id': 'sub', 'codex_cli_version': '9.9.9'},
                       {'exclusive_threads': True}, {'continuation_key': b'short'}, {'max_pending': True}]
        for i, options in enumerate(bad_options):
            target = self.root/f'never-created-{i}.sqlite'
            with self.assertRaisesRegex(LedgerError, 'composition_invalid'): self.bundle(bind=False, ledger_path=target, **options)
            self.assertFalse(target.exists())

    def test_aliases_cannot_add_ledger_tables_to_source_database(self):
        for target in (self.relay, self.root/'source-symlink', self.root/'source-hardlink'):
            if target.name == 'source-symlink': target.symlink_to(self.relay)
            if target.name == 'source-hardlink': os.link(self.relay, target)
            with self.assertRaisesRegex(LedgerError, 'composition_invalid'): self.bundle(bind=False, ledger_path=target)
        with sqlite3.connect(self.relay) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'usage_ledger%'").fetchone()[0], 0)

    def test_storage_failure_is_fixed_category_without_private_path(self):
        with self.assertRaisesRegex(LedgerError, '^ledger_composition_unavailable$'):
            self.bundle(bind=False, ledger_path=self.root/'synthetic-private-missing-dir'/'ledger.sqlite')

    def test_separate_producer_processes_keep_pending_and_restart_native_baseline(self):
        # Two actual interpreter lifetimes, with synthetic notifications only.
        script = '''
import json,socket,sys
from pathlib import Path
from backend.usage_ledger_composition import LedgerServices
from backend.tests.test_usage_ledger_composition import KEY,PREFIX_KEY,ROUTE,RAW,profile,usage
def forbidden(*a,**k):raise AssertionError('offline-only')
socket.socket.connect=forbidden;socket.getaddrinfo=forbidden
root=Path(sys.argv[1]);n=int(sys.argv[2])
s=LedgerServices(ledger_path=root/'ledger.sqlite',relay_db=root/'relay.sqlite',native_db=root/'native.sqlite',continuation_key=KEY,
 api_profiles=[profile()],fingerprint_key=PREFIX_KEY,codex_provider_id='synthetic-subscription',codex_cli_version='0.160.0',exclusive_threads=True)
if n==1:s.api.begin(ROUTE,'chat','a','crashed-before-terminal')
for method,params in [('turn/started',{'turn':{'id':f'turn-{n}'}}),('thread/tokenUsage/updated',{'turnId':f'turn-{n}','tokenUsage':usage(n)}),('turn/completed',{'turn':{'id':f'turn-{n}','status':'completed'}})]:
 s.codex.receive(method,{'threadId':'synthetic-thread',**params},epoch=1)
s.codex.reconcile(epoch=1)
'''
        for n in (1, 2):
            self.seed(n)
            result = subprocess.run([sys.executable, '-c', script, str(self.root), str(n)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        reader = self.bundle()
        rows = reader.ledger.records('codex')
        self.assertEqual(len(rows), 2); self.assertIsNone(rows[-1]['usage_baseline'])
        self.assertEqual(build_view(reader.ledger, 'api', 'a')['collection_health']['unconfirmed'], 1)


class CompositionRouteTests(CompositionFixture):
    # Exercise the installed reader with actual ASGI authentication; CI supplies dependencies.
    async def test_reader_mounts_once_uses_same_authority_and_requires_auth(self):
        import httpx
        from fastapi import FastAPI, HTTPException
        services = self.bundle(); app = FastAPI()
        def auth(request):
            if request.headers.get('authorization') != 'Bearer synthetic-only': raise HTTPException(401)
        services.install_reader(app, check_auth=auth)
        before = len(app.routes)
        with self.assertRaisesRegex(LedgerError, 'route_conflict'): services.install_reader(app, check_auth=auth)
        self.assertEqual(len(app.routes), before)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://offline') as client:
            self.assertEqual((await client.get('/app/sessions/a/usage-ledger')).status_code, 401)
            response = await client.get('/app/sessions/a/usage-ledger', headers={'Authorization': 'Bearer synthetic-only'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response.headers['cache-control'])
            self.assertEqual(response.json()['message_counts']['total'], 0)
            self.assertEqual((await client.get('/app/sessions/unknown/usage-ledger', headers={'Authorization': 'Bearer synthetic-only'})).status_code, 400)

    def test_existing_route_with_another_parameter_name_is_rejected_before_mount(self):
        from fastapi import FastAPI
        services = self.bundle(); app = FastAPI()
        @app.get('/app/sessions/{session_id}/usage-ledger')
        def existing(session_id): return {}
        before = len(app.routes)
        with self.assertRaisesRegex(LedgerError, 'route_conflict'): services.install_reader(app, check_auth=lambda r: None)
        self.assertEqual(len(app.routes), before)
