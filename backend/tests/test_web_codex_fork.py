import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

from backend import codex_generation_store as store
from backend.codex_generation_protocol import CodexProcessActivityGate, CodexGenerationError
from backend.web_codex_fork import fork_codex_conversation
from backend.web_session_fork import ForkError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_web_session_provider_authority import FakeLegacy


class WebCodexForkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.relay = self.root / 'relay.db'; self.db = self.root / 'jobs.db'
        self.legacy = FakeLegacy({'sessions':[{'id':'source','title':'原会话','provider':'codex'}], 'active_session':'source'})
        self.authority = WebSessionProviderAuthority(self.legacy); self.calls = []; self.fail = False
        store.initialize(self.db)
        store.pin_session(self.db, api_session='source', model='fixture-model', model_provider='openai', reasoning_effort='high', persona_hash=hashlib.sha256(b'persona').hexdigest())
        with closing(store.connect(self.db)) as conn:
            conn.execute("UPDATE codex_sessions SET thread_id='native-source',thread_attempt_id='original',cwd=?", (str(self.root),))
        with sqlite3.connect(self.relay) as conn:
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT, kind TEXT, text TEXT, meta TEXT)')
            for direction, kind, text in [('in','user','first'),('out','reply','answer1'),('in','user','typo'),('out','reply','answer2'),('in','user','later'),('out','reply','answer3')]:
                conn.execute('INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)', ('2026-10-04',direction,kind,text,json.dumps({'api_session':'source','provider':'codex','usage':{'totalTokens':20}})))
        for message_id in (1,3,5):
            job = store.enqueue_job(self.db,api_session='source',canonical_message_id=message_id,input_digest='a'*64,generation_id=f'g-{message_id}',client_message_id=f'c-{message_id}',callback_identity=f'r-{message_id}')
            with closing(store.connect(self.db)) as conn:
                conn.execute("UPDATE codex_generation_jobs SET status='completed',turn_id=?,assistant_message_id=? WHERE id=?", (f'turn-{message_id}',message_id+1,job['id']))
        async def qualify(*args): self.calls.append(('qualify',args))
        async def native_fork(**kwargs):
            self.calls.append(('fork',kwargs))
            if self.fail: raise CodexGenerationError('codex_generation_unavailable')
            return SimpleNamespace(thread_id='native-'+kwargs['api_session'],cwd=self.root/kwargs['api_session'])
        self.runtime = SimpleNamespace(generation_enabled=True,config=SimpleNamespace(store_path=self.db),persona_loader=lambda:'persona',foundation=SimpleNamespace(activity_gate=CodexProcessActivityGate(),generation=SimpleNamespace(qualify=qualify,fork_thread=native_fork)))
        self.body = {'request_id':str(uuid.uuid4()),'message_id':4,'mode':'branch'}
    async def fork(self, source='source', **changes):
        return await fork_codex_conversation(self.authority,source,{**self.body,**changes},relay_db=self.relay,runtime=self.runtime,lock=RLock())
    def rows(self, sid):
        with sqlite3.connect(self.relay) as conn:
            conn.row_factory=sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id", (sid,))]
    def native_calls(self): return [args for name,args in self.calls if name=='fork']
    async def test_branch_native_boundary_provider_model_usage_and_original_preserved(self):
        result=await self.fork(); sid=result['created']['id']
        self.assertEqual(self.native_calls()[0]['last_turn_id'],'turn-3')
        self.assertEqual(self.native_calls()[0]['thread_id'],'native-source')
        self.assertEqual([r['text'] for r in self.rows(sid)],['first','answer1','typo','answer2'])
        self.assertEqual(len(self.rows('source')),6); self.assertEqual(self.legacy.cfg['active_session'],'source')
        pin=store.get_session(self.db,sid)
        self.assertEqual((pin['provider'],pin['model'],pin['reasoning_effort']),('codex','fixture-model','high'))
        self.assertEqual(pin['thread_id'],'native-'+sid)
        self.assertTrue(all('usage' not in json.loads(r['meta']) for r in self.rows(sid)))
        with closing(store.connect(self.db)) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM codex_generation_jobs').fetchone()[0],3)
        store.initialize(self.db)  # No incompatible generation-store schema change.
    async def test_edit_regenerate_and_user_branch_have_exact_previous_turn(self):
        for mode, mid in [('edit',3),('regenerate',4),('branch',3)]:
            result=await self.fork(mode=mode,message_id=mid,request_id=str(uuid.uuid4()))
            self.assertEqual(self.native_calls()[-1]['last_turn_id'],'turn-1')
            self.assertEqual([r['text'] for r in self.rows(result['created']['id'])],['first','answer1'])
            self.assertEqual(result.get('draft'),'typo' if mode=='branch' else None)
    async def test_first_turn_edit_is_empty_and_never_copies_the_old_answer(self):
        result=await self.fork(mode='edit',message_id=1)
        self.assertEqual(self.rows(result['created']['id']),[]); self.assertEqual(self.native_calls(),[])
        self.assertIsNone(store.get_session(self.db,result['created']['id'])['thread_id'])
    async def test_branch_of_branch_uses_native_turn_binding_without_extra_usage_jobs(self):
        first=await self.fork(); sid=first['created']['id']; copied=self.rows(sid)
        second=await self.fork(source=sid,message_id=copied[-1]['id'],mode='regenerate',request_id=str(uuid.uuid4()))
        self.assertEqual(self.native_calls()[-1]['thread_id'],'native-'+sid)
        self.assertEqual(self.native_calls()[-1]['last_turn_id'],'turn-1')
        self.assertEqual([r['text'] for r in self.rows(second['created']['id'])],['first','answer1'])
    async def test_replay_conflict_and_uncertain_rpc_are_not_retried(self):
        first=await self.fork(); replay=await self.fork()
        self.assertTrue(replay['duplicate']); self.assertEqual(first['created'],replay['created']); self.assertEqual(len(self.native_calls()),1)
        with self.assertRaisesRegex(ForkError,'fork_request_conflict'):await self.fork(message_id=2)
        self.body['request_id']=str(uuid.uuid4()); self.fail=True
        with self.assertRaisesRegex(ForkError,'codex_fork_unavailable'):await self.fork()
        with self.assertRaisesRegex(ForkError,'codex_fork_result_unknown'):await self.fork()
        self.assertEqual(len(self.native_calls()),2)
    async def test_publication_failure_is_recoverable_without_repeating_native_fork_or_copy(self):
        publish=self.authority.publish_row
        self.authority.publish_row=lambda *args,**kwargs: (_ for _ in ()).throw(OSError('disk'))
        with self.assertRaises(OSError):await self.fork()
        self.authority.publish_row=publish
        result=await self.fork(); self.assertTrue(result['duplicate']); self.assertEqual(len(self.native_calls()),1)
        self.assertEqual(len(self.rows(result['created']['id'])),4)
    async def test_foreign_message_busy_missing_turn_and_disabled_fail_closed(self):
        with self.assertRaisesRegex(ForkError,'fork_message_not_found'):await self.fork(message_id=99)
        with closing(store.connect(self.db)) as conn:conn.execute("UPDATE codex_generation_jobs SET status='failed',assistant_message_id=NULL WHERE canonical_message_id=3")
        with self.assertRaisesRegex(ForkError,'codex_fork_history_unavailable'):await self.fork()
        self.runtime.generation_enabled=False
        with self.assertRaisesRegex(ForkError,'codex_generation_disabled'):await self.fork()
        self.assertEqual(self.calls,[])
    async def test_active_generation_blocks_fork_and_deleted_target_cannot_reappear(self):
        async with self.runtime.foundation.activity_gate.generation():
            with self.assertRaisesRegex(ForkError,'codex_generation_busy'):await self.fork()
        first=await self.fork()
        self.authority.tombstone_for_session=lambda sid: {'id':sid} if sid==first['created']['id'] else None
        with self.assertRaisesRegex(ForkError,'web_session_deleted'):await self.fork()
        self.assertEqual(len(self.native_calls()),1)
