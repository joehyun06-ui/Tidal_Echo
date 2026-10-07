"""Isolated API Web cancellation, durability and at-most-once generation checks."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.api_web_generation import ApiWebRuntime, ApiGenerationError, Store, assert_idle


class ApiWebGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'relay.db'
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
        self.started, self.release, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.calls = 0
        async def model(messages, **kwargs):
            self.calls += 1
            kwargs['on_route']({'model': 'fixture-model'})
            try:
                await kwargs['progress_sink']('哈'); await kwargs['progress_sink']('哈，已收到的正文')
                self.started.set(); await self.release.wait()
                return {'outcome':'success', 'model':'fixture-model', 'text':'完整回复', 'usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}}
            finally: self.closed.set()
        self.legacy = SimpleNamespace(RELAY_DB=self.path, run_model=model,
            build_ingest_messages=lambda text, **_: [{'role':'user','content':text}], relay_out=AsyncMock(return_value=(True,{},False)))
        self.runtime = ApiWebRuntime(self.legacy); self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)

    def accept(self, sid='a', text='hello', attachments=None):
        return self.runtime.store.accept(sid, text, {'api_session':sid,'channel':'web','source':'relay','attachments':attachments or []})

    async def ingest(self, row):
        return await self.runtime.ingest({'id':row['id'],'text':row['text'],'session_id':row['meta']['api_session']})

    async def drain(self):
        if self.runtime.tasks: await asyncio.gather(*list(self.runtime.tasks.values()))

    async def test_stop_exact_request_preserves_partial_once_and_closes_provider(self):
        row = self.accept(); ack = await self.ingest(row); await self.started.wait()
        self.assertEqual(self.runtime.status('a')['snapshot']['text'], '哈哈，已收到的正文')
        self.assertEqual((await self.ingest(row)), ack)
        with self.assertRaises(ApiGenerationError): await self.runtime.stop('other', ack['generation_id'])
        stopping = await self.runtime.stop('a', ack['generation_id'])
        self.assertEqual(stopping['generation']['status'], 'stopping')
        await self.runtime.stop('a', ack['generation_id']); await self.drain()
        self.assertTrue(self.closed.is_set()); self.assertEqual(self.calls, 1)
        status = self.runtime.status('a')['generation']
        self.assertEqual(status['status'], 'interrupted'); self.assertTrue(status['terminal'])
        self.assertTrue(status['upstream_result_unknown']); self.assertEqual(status['cancel_scope'], 'local_request')
        saved = self.runtime.store.notification('a', row['id'])
        self.assertEqual(saved['message']['text'], '哈哈，已收到的正文')
        self.assertEqual(saved['message']['meta']['api']['usage'], {})
        self.assertTrue(self.runtime.store.notification('a', row['id'])['duplicate'])
        await self.ingest(row); self.assertEqual(self.calls, 1)
        reopened = ApiWebRuntime(self.legacy); reopened.start()
        self.assertEqual(reopened.status('a')['generation']['assistant_message_id'], status['assistant_message_id'])

    async def test_queued_stop_and_busy_reject_before_new_canonical_input(self):
        row = self.accept()
        with self.assertRaisesRegex(ApiGenerationError, 'busy'): self.accept(text='duplicate')
        with self.runtime.store.db() as conn: self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        await self.runtime.stop('a', f"api-gen-{row['id']}")
        await self.ingest(row); await self.drain()
        self.assertEqual(self.calls, 0); self.assertIsNone(self.runtime.status('a')['generation']['assistant_message_id'])

    async def test_complete_then_late_stop_never_interrupts_next_turn(self):
        first = self.accept(); await self.ingest(first); await self.started.wait()
        self.release.set(); await self.drain()
        status = self.runtime.status('a')['generation']; self.assertEqual(status['status'],'completed')
        reply = self.runtime.store.notification('a',first['id'])['message']
        self.assertEqual(reply['meta']['api']['usage']['total_tokens'],16)
        self.assertGreaterEqual(reply['meta']['generation']['elapsed_ms'],0)
        second = self.accept(text='next')
        await self.runtime.stop('a',status['id'])
        self.assertEqual(self.runtime.status('a')['generation']['status'],'queued')
        self.assertEqual(self.runtime.status('a')['generation']['canonical_message_id'],second['id'])

    async def test_restart_and_expired_queue_are_terminal_without_redispatch(self):
        row = self.accept(); self.runtime.store.claim('a',row['id'],row['text'])
        reopened = ApiWebRuntime(self.legacy); reopened.start()
        state = reopened.status('a')['generation']
        self.assertEqual(state['status'],'failed'); self.assertTrue(state['upstream_result_unknown'])
        await reopened.ingest({'id':row['id'],'session_id':'a','text':row['text']})
        self.assertEqual(self.calls,0)
        row = self.accept('b')
        with self.runtime.store.db() as conn: conn.execute('UPDATE api_web_generations SET created_at=1 WHERE message_id=?',(row['id'],))
        self.assertEqual(self.runtime.status('b')['generation']['error'],'api_dispatch_not_started')
        await self.ingest(row); self.assertEqual(self.calls,0)

    async def test_authority_and_delete_guards_do_not_resurrect_content(self):
        row = self.accept()
        for body in ({'id':row['id'],'session_id':'a','text':'changed'}, {'id':row['id'],'session_id':'b','text':'hello'}, {'id':True,'session_id':'a','text':'hello'}):
            with self.assertRaises(ApiGenerationError): await self.runtime.ingest(body)
        with self.runtime.store.db() as conn:
            with self.assertRaises(ApiGenerationError): assert_idle(conn,'a',deleting=True)
        await self.runtime.stop('a',f"api-gen-{row['id']}")
        with self.runtime.store.db() as conn:
            assert_idle(conn,'a',deleting=True); conn.execute('DELETE FROM messages')
        with self.assertRaises(ApiGenerationError): self.accept()
        with self.assertRaises(ApiGenerationError): await self.ingest(row)

    async def test_missing_final_usage_failure_and_image_only_input(self):
        async def failure(messages, **kwargs):
            self.assertEqual(messages[-1]['content'],'')
            await kwargs['progress_sink']('部分内容')
            return {'outcome':'dispatch_uncertain','error':'private upstream exception'}
        self.legacy.run_model = failure
        row = self.accept(text='',attachments=[{'kind':'image','url':'/uploads/fixture.png'}])
        await self.ingest(row); await self.drain()
        state = self.runtime.status('a'); self.assertEqual(state['generation']['status'],'failed')
        self.assertNotIn('private',json.dumps(state))
        reply = self.runtime.store.notification('a',row['id'])['message']
        self.assertEqual(reply['text'],'部分内容'); self.assertEqual(reply['meta']['finish_reason'],'failed')
        self.assertEqual(reply['meta']['api']['usage'],{})

    async def test_notification_loss_does_not_repeat_model_or_insert_reply(self):
        self.legacy.relay_out.side_effect = lambda body: (False,{},True)
        row = self.accept(); await self.ingest(row); await self.started.wait()
        self.release.set(); await self.drain(); await self.ingest(row)
        self.assertEqual(self.runtime.status('a')['generation']['status'],'completed')
        self.assertEqual(self.calls,1)
        with self.runtime.store.db() as conn: self.assertEqual(conn.execute("SELECT count(*) FROM messages WHERE direction='out'").fetchone()[0],1)
