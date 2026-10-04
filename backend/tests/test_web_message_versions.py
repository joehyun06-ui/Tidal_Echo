import json
import sqlite3
import unittest
import uuid
from contextlib import closing

from backend import web_message_versions as versions
from backend import codex_generation_store as store
from backend.web_session_fork import fork_conversation, ForkError
from backend.tests import test_web_session_fork as api_fixture
from backend.tests import test_web_codex_fork as codex_fixture


class MessageVersionTests(unittest.TestCase):
    setUp = api_fixture.WebSessionForkTests.setUp
    rows = api_fixture.WebSessionForkTests.rows

    def version(self, source='a', message_id=4, mode='regenerate', request_id=None):
        body = dict(message_id=message_id, mode=mode, request_id=request_id or str(uuid.uuid4()))
        edge = versions.prepare(self.authority, source, body, relay_db=self.path)
        result = fork_conversation(self.authority, source, body, relay_db=self.path)
        versions.finish(self.authority, edge['version_id'], relay_db=self.path, select=True)
        return edge, result

    def test_reserved_copy_recovers_and_selection_is_durable_and_provider_bound(self):
        body = {**self.body, 'mode':'regenerate'}
        edge = versions.prepare(self.authority, 'a', body, relay_db=self.path)
        self.assertEqual(versions.public_state(self.authority, relay_db=self.path)['versions'], [])
        fork_conversation(self.authority, 'a', body, relay_db=self.path)
        state = versions.public_state(self.authority, relay_db=self.path)
        self.assertEqual(state['versions'], [edge])
        self.assertTrue(all(json.loads(meta)['version_copy'] for _,meta in self.rows(edge['version_id'])))
        versions.select_version(self.authority, 'a', {'version_id':edge['version_id']}, relay_db=self.path)
        self.assertEqual(versions.public_state(self.authority, relay_db=self.path)['selections'], [{'root_id':'a','version_id':edge['version_id']}])
        with self.assertRaises(ForkError):
            versions.select_version(self.authority, edge['version_id'], {'version_id':'a'}, relay_db=self.path)
        versions.select_version(self.authority, 'a', {'version_id':'a'}, relay_db=self.path)
        self.assertEqual(versions.prepare(self.authority, 'a', body, relay_db=self.path), edge)
        with self.assertRaises(ForkError):
            versions.prepare(self.authority, 'a', {**body, 'message_id':2}, relay_db=self.path)

    def test_repeated_regeneration_and_edits_share_points_without_merging_later_turns(self):
        edge, _ = self.version()
        sid = edge['version_id']
        self.assertEqual((edge['root_id'],edge['point_key'],edge['prompt_key']), ('a',4,3))
        with sqlite3.connect(self.path) as conn:
            for direction,kind,text in [('in','user','typo'),('out','reply','new answer'),('in','user','new later'),('out','reply','later answer')]:
                cur = conn.execute('INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)', ('today',direction,kind,text,json.dumps({'api_session':sid})))
                if text == 'new answer': mid = cur.lastrowid
            later = cur.lastrowid
        again, _ = self.version(sid, mid)
        self.assertEqual((again['root_id'],again['point_key'],again['prompt_key']), ('a',4,3))
        later_edge, _ = self.version(sid, later)
        self.assertEqual(later_edge['point_key'], later)
        copied = self.rows(later_edge['version_id'])
        self.assertEqual([json.loads(m)['version_key'] for _,m in copied], [1,2,3,4])
        self.assertEqual(json.loads(copied[1][1])['version_usage'], {'total_tokens':100})
        edit, _ = self.version(sid, mid-1, 'edit')
        self.assertEqual((edit['point_key'],edit['prompt_key']), (3,3))
        self.assertEqual(len(self.rows('a')),5)

    def test_group_delete_removes_alternatives_but_preserves_explicit_branches(self):
        first, _ = self.version()
        second, _ = self.version(message_id=3,mode='edit')
        branch = fork_conversation(self.authority,'a',{**self.body,'request_id':str(uuid.uuid4())},relay_db=self.path)['created']['id']
        result = versions.delete_versions(self.authority,'a',relay_db=self.path)
        self.assertEqual(set(result['deleted_ids']), {'a',first['version_id'],second['version_id']})
        self.assertFalse(result['deleted']['memory_deleted'])
        self.assertTrue(self.rows(branch))
        self.assertEqual(versions.public_state(self.authority,relay_db=self.path)['versions'], [])
        self.assertEqual(self.rows(first['version_id']), [])
        self.assertTrue(versions.delete_versions(self.authority,'a',relay_db=self.path)['deleted']['duplicate'])

    def test_explicit_branch_cannot_be_claimed_by_reusing_its_request_id(self):
        fork_conversation(self.authority,'a',self.body,relay_db=self.path)
        with self.assertRaises(ForkError):
            versions.prepare(self.authority,'a',{**self.body,'mode':'regenerate'},relay_db=self.path)
        for body in ({**self.body,'mode':'branch'},{**self.body,'message_id':True,'mode':'edit'},{**self.body,'request_id':'bad','mode':'edit'}):
            with self.assertRaises(ForkError):
                versions.prepare(self.authority,'a',body,relay_db=self.path)


class CodexMessageVersionTests(unittest.IsolatedAsyncioTestCase):
    setUp = codex_fixture.WebCodexForkTests.setUp
    rows = codex_fixture.WebCodexForkTests.rows
    fork = codex_fixture.WebCodexForkTests.fork
    native_calls = codex_fixture.WebCodexForkTests.native_calls

    async def test_same_group_uses_native_context_and_keeps_model_and_usage(self):
        body = {**self.body,'mode':'regenerate'}
        edge = versions.prepare(self.authority,'source',body,relay_db=self.relay)
        result = await self.fork(mode='regenerate')
        versions.finish(self.authority,result['created']['id'],relay_db=self.relay,select=True)
        self.assertEqual(edge['root_id'],'source')
        self.assertEqual(self.native_calls()[0]['last_turn_id'],'turn-1')
        self.assertEqual(store.get_session(self.db,edge['version_id'])['model'],'fixture-model')
        copied = self.rows(edge['version_id'])
        self.assertEqual(json.loads(copied[1]['meta'])['version_usage'], {'totalTokens':20})
        self.assertEqual(len(self.rows('source')),6)

    async def test_group_delete_preflights_every_native_job_before_touching_history(self):
        edge = versions.prepare(self.authority,'source',{**self.body,'mode':'regenerate'},relay_db=self.relay)
        await self.fork(mode='regenerate')
        versions.finish(self.authority,edge['version_id'],relay_db=self.relay)
        job = store.enqueue_job(self.db,api_session=edge['version_id'],canonical_message_id=999,input_digest='a'*64,generation_id='busy',client_message_id='busy',callback_identity='busy')
        with self.assertRaisesRegex(ForkError,'web_session_delete_job_active'):
            versions.delete_versions(self.authority,'source',relay_db=self.relay,codex_store=self.db)
        self.assertEqual(store.get_session(self.db,'source')['status'],'active')
        self.assertEqual(len(self.rows('source')),6)
        with closing(store.connect(self.db)) as conn:
            conn.execute("UPDATE codex_generation_jobs SET status='failed' WHERE id=?", (job['id'],))
        result = versions.delete_versions(self.authority,'source',relay_db=self.relay,codex_store=self.db)
        self.assertEqual(set(result['deleted_ids']), {'source',edge['version_id']})
        self.assertEqual(self.rows('source'), [])
