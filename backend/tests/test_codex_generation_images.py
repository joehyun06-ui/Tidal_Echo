"""Canonical uploads -> durable admission -> production worker -> App Server input."""
import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend import codex_generation_store as store
from backend.codex_canary_ingress import CodexCanaryIngressError
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegration, CodexCanaryLoopIntegrationError
from backend.codex_generation_images import ImageMessageInput, MAX_IMAGE_BYTES, load_image_web_message
from backend.codex_generation_live_reliability import enrich_generation_notification
from backend.codex_generation_protocol import CodexGenerationConfig, CodexGenerationProtocol, input_digest
from backend.codex_generation_streaming import StreamingCodexGenerationRuntime
from backend.tests.test_codex_canary_loop_integration import FakeLegacy
from backend.tests.test_codex_generation_live_reliability import FakeProtocol
from backend.tests import test_codex_generation_runtime as runtime_fixture

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGO4t/3YtrMHGCAUAEEWCT3AjKoyAAAAAElFTkSuQmCC')


class CodexImageInputTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.uploads = self.root / 'uploads'; self.uploads.mkdir()
        self.relay = self.root / 'relay.db'; self.path = self.uploads / 'att-abcdefghijklmA.png'; self.path.write_bytes(PNG)
        self.attachment = {'url':'/relay/uploads/' + self.path.name, 'kind':'image', 'mime':'image/png', 'size':len(PNG), 'name':'sample.png'}
        with sqlite3.connect(self.relay) as conn:
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, direction TEXT, kind TEXT, text TEXT, meta TEXT)')
        helper = runtime_fixture.CodexGenerationRuntimeTest(); self.config = helper.config(self.root, True)
        self.callback = AsyncMock(return_value=90)
        self.runtime = StreamingCodexGenerationRuntime(
            control_config=helper.control(self.root), generation_config=self.config, relay_db=self.relay,
            persona_loader=lambda:'persona', completion_callback=self.callback,
            progress_callback=AsyncMock(), upload_dir=self.uploads,
        )
        self.addAsyncCleanup(self.runtime.close)
        store.initialize(self.config.store_path)
        store.pin_session(self.config.store_path, api_session='api-canary', model='gpt-5.6-sol', model_provider='openai', reasoning_effort='high', persona_hash=hashlib.sha256(b'persona').hexdigest())
        legacy = FakeLegacy(); legacy.RELAY_DB = str(self.relay); legacy.add_session('api-canary', 'codex')
        self.integration = CodexCanaryLoopIntegration(legacy, self.runtime)
        self.calls = []
        owner = self
        class Transport:
            async def request(self, method, params):
                owner.calls.append((method, params))
                turn_id = 'turn-' + str(len(owner.calls))
                await owner.runtime.event_inbox.on_event(enrich_generation_notification('turn/completed', {
                    'threadId':'thr-1', 'turn':{'id':turn_id, 'status':'completed', 'items':[
                        {'type':'agentMessage', 'id':'reply', 'text':'fixture image received', 'phase':'final_answer'}
                    ]},
                }))
                return {'turn':{'id':turn_id, 'status':'inProgress'}}
        protocol = FakeProtocol(self.root / 'workspace')
        wire = CodexGenerationProtocol(CodexGenerationConfig(True, self.root / 'workspace'), Transport())
        protocol.start_turn = wire.start_turn
        self.runtime.worker.protocol = protocol

    def save(self, text='', attachments=None, mid=1, **meta_overrides):
        meta = {'api_session':'api-canary', 'channel':'web', 'source':'relay', 'attachments':attachments if attachments is not None else [self.attachment], **meta_overrides}
        with sqlite3.connect(self.relay) as conn:
            conn.execute('INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?)', (mid, 'in', 'user', text, json.dumps(meta)))

    def load(self, **kwargs):
        return load_image_web_message(self.relay, canonical_message_id=1, api_session='api-canary', upload_dir=self.uploads, **kwargs)

    async def test_image_only_and_caption_followup_reach_real_protocol_once(self):
        for mid, text in enumerate(('', '看这张图'), 1):
            self.save(text, mid=mid)
            ack = await self.integration.handle_ingest({'id':mid, 'text':text, 'session_id':'api-canary'})
            duplicate = await self.integration.handle_ingest({'id':mid, 'text':text, 'session_id':'api-canary'})
            self.assertEqual(ack['generation_id'], duplicate['generation_id'])
            self.assertTrue(await self.runtime.worker.run_once())
            items = self.calls[-1][1]['input']
            self.assertEqual(items[-1], {'type':'localImage', 'path':str(self.path)})
            self.assertEqual(len(items), 2 if text else 1)
            if text: self.assertEqual(items[0], {'type':'text', 'text':text})
            self.assertEqual(self.calls[-1][1]['clientUserMessageId'], f'codex-client-{mid}')
            self.assertEqual(store.get_job(self.config.store_path, mid)['status'], 'completed')
            self.assertFalse(await self.runtime.worker.run_once())
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.callback.await_count, 2)

    async def test_image_replaced_or_missing_after_admission_never_dispatches(self):
        self.save('caption')
        await self.integration.handle_ingest({'id':1, 'text':'caption', 'session_id':'api-canary'})
        self.path.write_bytes(PNG[:-1] + b'x')
        await self.runtime.worker.run_once()
        self.assertEqual(self.calls, [])
        self.assertEqual(store.get_job(self.config.store_path, 1)['error_category'], 'codex_canary_input_contract_changed')
        self.path.unlink(); self.save(mid=2)
        with self.assertRaisesRegex(CodexCanaryLoopIntegrationError, 'codex_image_unavailable'):
            await self.integration.handle_ingest({'id':2, 'text':'', 'session_id':'api-canary'})

    async def test_remote_url_traversal_symlink_bad_mime_and_oversize_rejected(self):
        for url in ('https://example.com/relay/uploads/'+self.path.name, '/relay/uploads/../secret.png', '/relay/uploads/%2e%2e/secret.png', '/relay/uploads/'+self.path.name+'?token=private'):
            self.save(attachments=[{**self.attachment, 'url':url}])
            with self.assertRaisesRegex(CodexCanaryIngressError, 'codex_image_reference_invalid'): self.load()
        self.save(attachments=[{**self.attachment, 'mime':'image/jpeg'}])
        with self.assertRaisesRegex(CodexCanaryIngressError, 'codex_image_metadata_invalid'): self.load()
        self.save(); self.path.unlink(); self.path.symlink_to(self.relay)
        with self.assertRaisesRegex(CodexCanaryIngressError, 'codex_image_unavailable'): self.load()
        self.path.unlink()
        with self.path.open('wb') as handle: handle.truncate(MAX_IMAGE_BYTES + 1)
        with self.assertRaisesRegex(CodexCanaryIngressError, 'codex_image_too_large'): self.load()
        self.assertEqual(self.calls, [])

    async def test_canonical_scope_text_only_compatibility_and_large_rpc_bound(self):
        self.save('ordinary text', [])
        self.assertEqual(self.load(), 'ordinary text')
        self.assertEqual(input_digest(self.load()), hashlib.sha256(b'ordinary text').hexdigest())
        for override in ({'api_session':'other'}, {'channel':'telegram'}, {'source':'external'}):
            self.save(**override)
            with self.assertRaises(CodexCanaryIngressError): self.load()
        self.save('caption')
        with self.assertRaisesRegex(CodexCanaryIngressError, 'contract_changed'): self.load(expected_text='changed')
        self.path.write_bytes(PNG + b'\0' * (2 * 1024 * 1024))
        self.save(attachments=[{**self.attachment, 'size':self.path.stat().st_size}])
        loaded = self.load(); self.assertIsInstance(loaded, ImageMessageInput)
        self.assertLess(len(json.dumps(loaded.wire_items()).encode()), 1024)
        self.assertEqual(loaded.images[0].sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
