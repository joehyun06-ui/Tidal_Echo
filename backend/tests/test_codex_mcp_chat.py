"""Synthetic native calls only; never contact a model or external plugin."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

from backend import codex_mcp_chat as native, codex_generation_store as jobs
from backend import web_mcp_chat as mcp, web_mcp_chat_routes as routes, web_mcp_settings as settings
from backend.codex_generation_worker import CodexGenerationEventInbox
from backend.codex_generation_protocol import GenerationNotification, CodexProcessActivityGate
from backend.tests import test_codex_app_server_shared_transport as transport_fixture

PLUGIN = {'id': 'fixture', 'serverId': 'fixture', 'name': '合成工具', 'transport': 'streamable-http',
          'enabled': True, 'url': 'https://example.com/mcp', 'options': {}}
TOOL = {'name': 'lookup', 'description': 'synthetic only', 'inputSchema': {
    'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}}


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / 'relay.db'; self.gen = self.root / 'codex.db'
        jobs.initialize(self.gen)
        jobs.pin_session(self.gen, api_session='chat', model='fixture', model_provider='openai', reasoning_effort=None,
                         persona_hash=hashlib.sha256(b'persona').hexdigest())
        self.manager = native.Manager(self.path, self.gen); self.store = self.manager.store
        settings.save(self.path, {'revision': 0, 'plugins': [PLUGIN]})
        self.store.save('chat', {'revision': 0, 'config_revision': 1, 'plugin_ids': ['fixture']})
        jobs.enqueue_job(self.gen, api_session='chat', canonical_message_id=42, input_digest='a' * 64,
                         generation_id='codex-gen-42', client_message_id='client-42', callback_identity='callback-42')
        self.job = jobs.claim_next_job(self.gen)
        jobs.begin_thread_dispatch(self.gen, job_id=self.job['id'], thread_attempt_id='attempt', cwd=str(self.root))
        jobs.bind_session_thread(self.gen, job_id=self.job['id'], thread_attempt_id='attempt', thread_id='thread', cwd=str(self.root))
        self.manager.record_thread('thread/start', {}, {'thread': {'id': 'thread'}})
        jobs.begin_turn_dispatch(self.gen, job_id=self.job['id'])
        jobs.record_turn_started(self.gen, job_id=self.job['id'], turn_id='turn')
        self.calls = []
        self.execution_fails = False
        def wire(_plugin, **kw):
            if kw.get('call'):
                kw['before_call'](); self.calls.append(kw['call'])
                if self.execution_fails:
                    raise OSError('private synthetic provider detail')
                return {'content': [{'type': 'text', 'text': 'synthetic result'}]}
            return {'tools': [TOOL], 'truncated': False}
        self.wire = patch.object(mcp.wire, 'discover', side_effect=wire); self.network = self.wire.start(); self.addCleanup(self.wire.stop)
        await self.manager.begin(self.job)
        self.addAsyncCleanup(self.manager.finish)
        self.alias = self.manager.context.definitions()[0]['name']

    def params(self, **changes):
        return {'threadId': 'thread', 'turnId': 'turn', 'callId': 'call-1', 'namespace': None,
                'tool': 'guiting_plugin_call', 'arguments': {'name': self.alias, 'arguments': {'query': 'fixture'}}, **changes}

    async def pending(self):
        for _ in range(100):
            rows = self.store.actions('chat')['actions']
            if rows:
                return rows[-1]
            await asyncio.sleep(.01)
        self.fail('proposal missing')

    async def test_native_list_and_approved_call_are_correlated_and_one_use(self):
        value = await self.manager.handle(self.params(tool='guiting_plugin_list', arguments={}))
        self.assertTrue(value['success']); self.assertIn(self.alias, value['contentItems'][0]['text'])
        task = asyncio.create_task(self.manager.handle(self.params(callId='call-2')))
        row = await self.pending()
        self.assertEqual(row['generation_id'], 'codex-gen-42'); self.assertEqual(row['message_id'], 42)
        self.assertEqual(self.calls, [])
        self.store.decision('chat', row['id'], {'revision': row['revision'], 'decision': 'approve'})
        self.assertTrue((await task)['success']); self.assertEqual(len(self.calls), 1)
        self.assertFalse((await self.manager.handle(self.params(callId='call-2')))['success'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.actions('chat')['actions'][0]['status'], 'completed')
        with self.assertRaises(settings.SettingsError):
            self.store.decision('other', row['id'], {'revision': 0, 'decision': 'approve'})

    async def test_denial_returns_tool_output_without_execution(self):
        task = asyncio.create_task(self.manager.handle(self.params()))
        row = await self.pending()
        self.assertFalse((await self.manager.handle(self.params(callId='parallel')))['success'])
        self.store.decision('chat', row['id'], {'revision': 0, 'decision': 'deny'})
        result = await task
        self.assertFalse(result['success']); self.assertIn('denied', result['contentItems'][0]['text'])
        self.assertEqual(self.calls, [])

    async def test_request_before_turn_reply_waits_for_durable_exact_turn(self):
        from contextlib import closing
        with closing(jobs.connect(self.gen)) as conn:
            conn.execute("UPDATE codex_generation_jobs SET status='turn_dispatching',turn_id=NULL WHERE id=?", (self.job['id'],))
        task = asyncio.create_task(self.manager.handle(self.params()))
        await asyncio.sleep(.06); self.assertEqual(self.store.actions('chat')['actions'], [])
        jobs.record_turn_started(self.gen, job_id=self.job['id'], turn_id='turn')
        row = await self.pending(); self.store.decision('chat', row['id'], {'revision': 0, 'decision': 'deny'})
        self.assertFalse((await task)['success']); self.assertEqual(self.calls, [])

    async def test_stop_and_recovery_do_not_replay(self):
        task = asyncio.create_task(self.manager.handle(self.params()))
        row = await self.pending(); self.manager.stop('chat', 'codex-gen-42')
        self.assertFalse((await task)['success']); self.assertEqual(self.store.state(row['id'])['status'], 'cancelled')
        self.assertFalse((await self.manager.handle(self.params(callId='later')))['success'])
        self.assertEqual(self.calls, [])
        self.store.recover(); self.assertEqual(self.store.state(row['id'])['status'], 'cancelled')

    async def test_ambiguous_execution_never_retries_or_leaks_error(self):
        self.execution_fails = True
        task = asyncio.create_task(self.manager.handle(self.params())); row = await self.pending()
        self.store.decision('chat', row['id'], {'revision': 0, 'decision': 'approve'})
        result = await task
        self.assertFalse(result['success']); self.assertNotIn('private synthetic', json.dumps(result))
        self.assertEqual(self.store.state(row['id'])['status'], 'unknown'); self.assertEqual(len(self.calls), 1)
        self.store.mark(row['id'], 'completed', 'late result')
        self.assertEqual(self.store.state(row['id'])['status'], 'unknown')

    async def test_mismatched_turn_unknown_tool_bad_schema_and_parallel_call_rejected(self):
        for i, params in enumerate([self.params(turnId='old'), self.params(threadId='other'), self.params(namespace='shell'),
                       self.params(tool='command/exec'), self.params(arguments={'name': 'unknown', 'arguments': {}}),
                       self.params(arguments={'name': self.alias, 'arguments': {'query': 42}})]):
            params['callId'] = f'bad-{i}'
            result = await self.manager.handle(params); self.assertFalse(result['success'])
        self.assertEqual(self.store.actions('chat')['actions'], []); self.assertEqual(self.calls, [])

    async def test_selection_change_and_restart_cancel_only_native_actions(self):
        task = asyncio.create_task(self.manager.handle(self.params())); row = await self.pending()
        # The API runtime has its own action table and cannot cancel this call.
        mcp.Store(self.path).recover(); self.assertEqual(self.store.state(row['id'])['status'], 'pending')
        self.store.save('chat', {'revision': 1, 'config_revision': 1, 'plugin_ids': []})
        self.assertFalse((await task)['success']); self.assertEqual(self.calls, [])
        self.assertEqual(self.store.state(row['id'])['status'], 'cancelled')

    async def test_default_off_old_thread_capability_and_inherited_fork(self):
        await self.manager.finish()
        self.store.save('chat', {'revision': 1, 'config_revision': 1, 'plugin_ids': []})
        self.network.reset_mock(); await self.manager.begin(self.job)
        self.network.assert_not_called(); self.assertIsNone(self.manager.context)
        with self.store.db() as conn:
            conn.execute("DELETE FROM web_mcp_codex_threads WHERE thread_id='thread'")
        self.assertFalse(self.manager.supported('chat'))
        self.manager.record_thread('thread/fork', {'threadId': 'thread'}, {'thread': {'id': 'old-fork'}})
        with self.store.db() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM web_mcp_codex_threads WHERE thread_id='old-fork'").fetchone())
        self.manager.record_thread('thread/start', {}, {'thread': {'id': 'thread'}})
        self.manager.record_thread('thread/fork', {'threadId': 'thread'}, {'thread': {'id': 'new-fork'}})
        with self.store.db() as conn:
            self.assertTrue(conn.execute("SELECT 1 FROM web_mcp_codex_threads WHERE thread_id='new-fork'").fetchone())

    async def test_native_routes_auth_capability_revision_and_no_store(self):
        app = FastAPI()
        def auth(request):
            if request.headers.get('Authorization') != 'Bearer synthetic': raise HTTPException(401)
        integration = SimpleNamespace(_session_lock=__import__('threading').RLock(),
                                      session_authority=SimpleNamespace(row_for_session=lambda sid: {'provider': 'codex'} if sid == 'chat' else None))
        routes.install_loop(app, SimpleNamespace(RELAY_DB=self.path, check_internal_auth=auth), integration,
                            SimpleNamespace(chat_mcp=self.manager, generation_enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
            self.assertEqual((await client.get('/loop/sessions/chat/mcp')).status_code, 401)
            response = await client.get('/loop/sessions/chat/mcp', headers={'Authorization': 'Bearer synthetic'})
            self.assertTrue(response.json()['supported']); self.assertIn('no-store', response.headers['cache-control'])
            with self.store.db() as conn: conn.execute('DELETE FROM web_mcp_codex_threads')
            response = await client.get('/loop/sessions/chat/mcp', headers={'Authorization': 'Bearer synthetic'})
            self.assertEqual(response.json()['reason'], 'codex_new_session_required')

    async def test_approval_wait_pauses_active_timeout_and_terminal_is_immediate(self):
        inbox = CodexGenerationEventInbox(); manager = SimpleNamespace(running={True})
        aware = native.ToolAwareInbox(inbox, manager)
        task = asyncio.create_task(aware.wait_terminal('turn', timeout_seconds=.03))
        await asyncio.sleep(.08); self.assertFalse(task.done())
        await inbox.on_event(GenerationNotification('turn/completed', 'thread', 'turn', True))
        event, _ = await task; self.assertTrue(event.terminal)

    async def test_approved_native_tool_then_final_reply_completes_same_generation(self):
        from unittest.mock import AsyncMock
        from backend.codex_generation_live_reliability import RichGenerationNotification
        inbox = CodexGenerationEventInbox(); received = []
        async def complete(job, answer, usage):
            received.append((job['generation_id'], answer)); return 77
        worker = native.McpCodexWorker(store_path=self.gen,
            protocol=SimpleNamespace(unsubscribe=AsyncMock()), activity_gate=CodexProcessActivityGate(),
            persona_loader=lambda: 'persona', canonical_message_loader=lambda job: 'fixture',
            completion_callback=complete, event_inbox=native.ToolAwareInbox(inbox, self.manager), turn_timeout_seconds=.05)
        session = jobs.get_session(self.gen, 'chat')
        call = asyncio.create_task(self.manager.handle(self.params())); row = await self.pending()
        reply = asyncio.create_task(worker._await_or_recover_terminal(self.job['id'], 'turn', session, 'persona'))
        await asyncio.sleep(.08); self.assertFalse(reply.done())
        self.store.decision('chat', row['id'], {'revision': 0, 'decision': 'approve'})
        self.assertTrue((await call)['success'])
        await inbox.on_event(RichGenerationNotification('turn/completed', 'thread', 'turn', True,
                                                       turn_status='completed', final_answer='合成工具已完成'))
        await reply
        self.assertEqual(received, [('codex-gen-42', '合成工具已完成')])
        self.assertEqual(jobs.get_job(self.gen, self.job['id'])['status'], 'completed')
        self.assertEqual(len(self.calls), 1)

    async def test_native_history_is_removed_when_conversation_deleted(self):
        from backend.api_web_generation import assert_idle
        task = asyncio.create_task(self.manager.handle(self.params())); row = await self.pending()
        self.store.decision('chat', row['id'], {'revision': 0, 'decision': 'deny'}); await task
        with self.store.db() as conn: assert_idle(conn, 'chat', deleting=True)
        self.assertEqual(self.store.actions('chat')['actions'], [])


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    setUp = transport_fixture.SharedTransportTest.setUp
    asyncTearDown = transport_fixture.SharedTransportTest.asyncTearDown
    runtime = transport_fixture.SharedTransportTest.runtime

    async def test_tool_reply_write_failure_does_not_cancel_or_await_itself(self):
        runtime = self.runtime(timeout=3); scope = runtime.scope(methods=frozenset({'account/read'}))
        await scope.request('account/read', {})
        async def handler(params): return native.output({'ok': True})
        runtime.install_tool_handler(handler)
        owner = runtime._process
        with patch.object(owner.stdin, 'write', side_effect=BrokenPipeError):
            await runtime._consume_message({'id': 'tool-1', 'method': 'item/tool/call', 'params': {}}, owner=owner)
            pending = runtime._tool_tasks['tool-1'][1]
            await asyncio.wait_for(pending, 3)
        self.assertIsNone(runtime._process)
        self.assertFalse(runtime._tool_tasks)

    async def test_handler_does_not_block_reader_and_process_exit_cancels_pending_tool(self):
        runtime = self.runtime(timeout=3); scope = runtime.scope(methods=frozenset({'account/read'}))
        await scope.request('account/read', {})
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(params):
            entered.set(); await release.wait(); return native.output({'ok': True})
        runtime.install_tool_handler(handler)
        with self.assertRaises(Exception): runtime.install_tool_handler(handler)
        owner = runtime._process
        await runtime._consume_message({'id': 'tool-1', 'method': 'item/tool/call', 'params': {}}, owner=owner)
        await entered.wait()
        self.assertEqual((await scope.request('account/read', {}))['account']['type'], 'chatgpt')
        pending = list(runtime._tool_tasks.values())[0][1]
        await runtime.close(); await asyncio.gather(pending, return_exceptions=True)
        self.assertTrue(pending.cancelled())
