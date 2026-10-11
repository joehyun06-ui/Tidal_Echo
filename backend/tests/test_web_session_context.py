import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from backend import web_session_context as context
from backend.api_web_generation import input_messages
from backend.codex_generation_images import load_image_web_message, ImageMessageInput
from backend.codex_canary_ingress import CodexCanaryIngressError
from backend.codex_generation_protocol import input_digest


class SessionContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'relay.sqlite')
        self.uploads = Path(self.tmp.name)
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, ts TEXT, direction TEXT, kind TEXT, text TEXT, meta TEXT)')
        self.config = {**context.defaults(), 'user_name': '测试称呼', 'tone': '自然一些', 'project': '合成测试项目'}

    def message(self, sid='a', *, snapshot=None, text='合成问题', ts='2026-10-11T05:00:00+00:00', direction='in', attachments=None):
        meta = {'api_session': sid, 'channel': 'web', 'source': 'relay', 'attachments': attachments or []}
        if snapshot is not None:
            meta[context.META_KEY] = snapshot
        with sqlite3.connect(self.db) as conn:
            return conn.execute('INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,?,?,?,?)',
                (ts, direction, 'user' if direction == 'in' else 'reply', text, json.dumps(meta))).lastrowid

    def test_persist_revision_conflict_and_session_isolation(self):
        self.assertEqual(context.get(self.db, 'a')['revision'], 0)
        saved = context.save(self.db, 'a', {'revision': 0, 'config': self.config})
        self.assertEqual(context.get(self.db, 'a'), saved)
        with self.assertRaisesRegex(context.ContextError, 'revision_conflict'):
            context.save(self.db, 'a', {'revision': 0, 'config': {**self.config, 'tone': 'stale'}})
        self.assertEqual(context.get(self.db, 'a')['config']['tone'], '自然一些')
        self.assertEqual(context.get(self.db, 'b')['config']['user_name'], '')

    def test_versions_share_and_branch_copy_is_independent_idempotent(self):
        context.save(self.db, 'a', {'revision': 0, 'config': self.config})
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY, root_id TEXT)')
            conn.execute("INSERT INTO web_message_versions VALUES ('version-a','a')")
        context.copy_branch(self.db, 'a', 'version-a')
        context.save(self.db, 'version-a', {'revision': 1, 'config': {**self.config, 'project': '新版进度'}})
        self.assertEqual(context.get(self.db, 'a')['config']['project'], '新版进度')
        context.copy_branch(self.db, 'version-a', 'branch-b')
        context.save(self.db, 'branch-b', {'revision': 1, 'config': {**self.config, 'tone': '分支语气'}})
        context.copy_branch(self.db, 'version-a', 'branch-b')
        self.assertEqual(context.get(self.db, 'branch-b')['config']['tone'], '分支语气')
        self.assertEqual(context.get(self.db, 'a')['config']['tone'], '自然一些')
        with context.database(self.db) as conn:
            context.delete(conn, 'version-a')
        self.assertEqual(context.get(self.db, 'a')['revision'], 2)
        with context.database(self.db) as conn:
            context.delete(conn, 'a')
        self.assertEqual(context.get(self.db, 'a')['revision'], 0)
        self.assertEqual(context.get(self.db, 'branch-b')['revision'], 2)

    def test_snapshot_freezes_values_and_disabled_sections_do_not_leave_text(self):
        context.save(self.db, 'a', {'revision': 0, 'config': self.config})
        frozen = context.snapshot(self.db, 'a', 'Asia/Shanghai')
        context.save(self.db, 'a', {'revision': 1, 'config': {**self.config, **dict.fromkeys(context.SWITCHES, False)}})
        off = context.snapshot(self.db, 'a', 'Asia/Shanghai')
        self.assertIn('测试称呼', context.prompt({context.META_KEY: frozen}))
        self.assertEqual({k: off[k] for k in ('state', 'relationship', 'time')}, dict.fromkeys(('state', 'relationship', 'time')))
        self.assertNotIn('测试称呼', context.prompt({context.META_KEY: off}))
        self.assertEqual(context.get(self.db, 'a')['config']['user_name'], '测试称呼')

    def test_clock_uses_current_session_and_dst_aware_offset(self):
        self.message('a', ts='2026-11-01T08:45:00+00:00')
        self.message('b', ts='2026-11-01T09:14:00+00:00')
        now = datetime(2026, 11, 1, 9, 15, tzinfo=timezone.utc)
        clock = context.snapshot(self.db, 'a', 'America/Los_Angeles', now=now)['time']
        self.assertEqual(clock['gap_seconds'], 1800)
        self.assertEqual(clock['local_time'], '2026-11-01T01:15:00-08:00')
        self.assertEqual(clock['timezone'], 'America/Los_Angeles')
        self.assertNotIn('gap_seconds', context.snapshot(self.db, 'empty', 'invalid', now=now)['time'])
        context.save(self.db, 'a', {'revision': 0, 'config': {**self.config, 'timezone': 'Asia/Shanghai'}})
        self.assertEqual(context.snapshot(self.db, 'a', 'America/Los_Angeles', now=now)['time']['timezone'], 'Asia/Shanghai')

    def test_invalid_input_and_corrupt_snapshot_fail_closed(self):
        for config in ({}, {**self.config, 'state_enabled': 1}, {**self.config, 'tone': 'a'*501}, {**self.config, 'timezone': '../../etc/passwd'}, {**self.config, 'project': 'a\0b'}):
            with self.assertRaises(context.ContextError):
                context.save(self.db, 'a', {'revision': 0, 'config': config})
        with self.assertRaises(context.ContextError):
            context.save(self.db, 'a', {'revision': False, 'config': self.config})
        self.assertEqual(context.get(self.db, 'a')['revision'], 0)
        with self.assertRaises(context.ContextError):
            context.prompt({context.META_KEY: {'version': 1}})

    def test_api_and_codex_use_same_frozen_background_without_changing_raw_text(self):
        context.save(self.db, 'a', {'revision': 0, 'config': self.config})
        frozen = context.snapshot(self.db, 'a', 'Asia/Shanghai')
        mid = self.message(snapshot=frozen)
        legacy = SimpleNamespace(RELAY_DB=self.db, build_ingest_messages=lambda text, **kw: [{'role': 'system', 'content': 'original persona'}, {'role': 'user', 'content': text}])
        messages = input_messages(legacy, 'a', mid, '合成问题')
        codex = load_image_web_message(self.db, canonical_message_id=mid, api_session='a', upload_dir=self.uploads, expected_text='合成问题', include_context=True)
        self.assertEqual(messages[0]['content'], 'original persona')
        self.assertEqual(messages[-1]['content'], '合成问题')
        self.assertEqual(messages[-2]['role'], 'user')
        self.assertTrue(codex.startswith(messages[-2]['content']))
        digest = input_digest(codex)
        context.save(self.db, 'a', {'revision': 1, 'config': {**self.config, 'tone': 'future'}})
        again = load_image_web_message(self.db, canonical_message_id=mid, api_session='a', upload_dir=self.uploads, expected_digest=digest, include_context=True)
        self.assertEqual(again, codex)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT text FROM messages WHERE id=?', (mid,)).fetchone()[0], '合成问题')
            meta = json.loads(conn.execute('SELECT meta FROM messages WHERE id=?', (mid,)).fetchone()[0])
            meta[context.META_KEY]['revision'] = 999
            conn.execute('UPDATE messages SET meta=? WHERE id=?', (json.dumps(meta), mid))
        with self.assertRaises(CodexCanaryIngressError):
            load_image_web_message(self.db, canonical_message_id=mid, api_session='a', upload_dir=self.uploads, expected_digest=digest, include_context=True)
        with self.assertRaises(CodexCanaryIngressError):
            load_image_web_message(self.db, canonical_message_id=mid, api_session='b', upload_dir=self.uploads, include_context=True)

    def test_codex_image_only_keeps_local_image_and_adds_context(self):
        import base64
        data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAD0lEQVR4nGP4z8DAwMAARQAFAAH/VxQxAAAAAElFTkSuQmCC')
        image = self.uploads / 'att-abcdefghijklmn.png'
        image.write_bytes(data)
        mid = self.message(text='', snapshot=context.snapshot(self.db, 'a', 'UTC'), attachments=[{'url': '/relay/uploads/'+image.name, 'kind': 'image', 'mime': 'image/png', 'size': len(data)}])
        value = load_image_web_message(self.db, canonical_message_id=mid, api_session='a', upload_dir=self.uploads, expected_text='', include_context=True)
        self.assertIsInstance(value, ImageMessageInput)
        self.assertEqual(value.wire_items()[1], {'type': 'localImage', 'path': str(image)})
        self.assertIn('本轮上下文', value.wire_items()[0]['text'])

    def test_legacy_without_snapshot_retains_exact_input(self):
        mid = self.message()
        self.assertEqual(load_image_web_message(self.db, canonical_message_id=mid, api_session='a', upload_dir=self.uploads, include_context=True), '合成问题')

    def test_codex_wire_uses_context_as_turn_input_without_changing_permissions(self):
        import asyncio
        from backend.codex_generation_protocol import CodexGenerationConfig, CodexGenerationProtocol
        frozen = context.snapshot(self.db, 'a', 'UTC')
        mid = self.message(snapshot=frozen)
        value = load_image_web_message(self.db, canonical_message_id=mid, api_session='a', upload_dir=self.uploads, include_context=True)
        calls = []
        async def request(method, params):
            calls.append((method, params))
            return {'turn': {'id': 'turn-fixture', 'status': 'inProgress'}}
        protocol = CodexGenerationProtocol(CodexGenerationConfig(True, self.uploads), SimpleNamespace(request=request))
        asyncio.run(protocol.start_turn(thread_id='thread-fixture', client_message_id='context-fixture', text=value, model='fixture-model', reasoning_effort=None))
        method, params = calls[0]
        self.assertEqual(method, 'turn/start')
        self.assertEqual(params['input'], [{'type': 'text', 'text': value}])
        self.assertEqual(params['environments'], [])
        self.assertNotIn('baseInstructions', params)
        self.assertNotIn('approvalPolicy', params)


if __name__ == '__main__':
    unittest.main()
