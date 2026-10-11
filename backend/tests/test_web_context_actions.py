import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from backend import codex_generation_store as store, web_session_context as context
from backend.codex_generation_protocol import CodexProcessActivityGate
from backend.web_codex_compaction import Compactions, CompactionError
from backend.web_continuation import preview


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'jobs.db'
        self.relay = Path(temp.name) / 'relay.db'
        self.persona = 'synthetic persona'
        store.initialize(self.path)
        for sid in ('a','b'):
            store.pin_session(self.path,api_session=sid,model='fixture',model_provider='openai',reasoning_effort=None,
                              persona_hash=hashlib.sha256(self.persona.encode()).hexdigest())
        with closing(store.connect(self.path)) as conn:
            conn.execute("UPDATE codex_sessions SET thread_id='thread-'||api_session,thread_attempt_id='attempt-'||api_session,cwd=?",(temp.name,))
        self.calls=[]; self.page={'data':[{'id':'old-turn','status':'completed','items':[]}]}; self.behavior='ack'
        async def resume(**kwargs):
            self.calls.append(('resume',kwargs)); return self.page
        async def request(method,params):
            self.calls.append((method,params))
            if self.behavior in {'complete','cancel_after_complete'}:
                self.event('turn/started',turn={'id':'new-turn','status':'inProgress'})
                self.event('item/completed',turnId='new-turn',item={'type':'contextCompaction','text':'never persist'})
                self.event('turn/completed',turn={'id':'new-turn','status':'completed'})
            if self.behavior=='cancel_after_complete': raise asyncio.CancelledError()
            if self.behavior=='error': raise OSError('synthetic transport failure')
            if self.behavior=='hold': await asyncio.Event().wait()
            return {}
        def scope(**kwargs):
            self.assertEqual(kwargs['methods'],frozenset({'thread/compact/start'}))
            self.handler=kwargs['handler']; return SimpleNamespace(request=request)
        self.runtime=SimpleNamespace(config=SimpleNamespace(store_path=self.path),relay_db=self.relay,
            persona_loader=lambda:self.persona,foundation=SimpleNamespace(runtime=SimpleNamespace(scope=scope),
            generation=SimpleNamespace(resume_thread=resume),activity_gate=CodexProcessActivityGate()))
        self.manager=Compactions(self.runtime,timeout=0.01); self.manager.initialize()
        self.body={'request_id':str(uuid.uuid4())}

    def event(self,method,**params): self.handler(method,{'threadId':'thread-a',**params})
    def state(self): return self.manager.status('a')['operation']['status']
    def enqueue(self,sid='a'):
        return store.enqueue_job(self.path,api_session=sid,canonical_message_id=1,input_digest='a'*64,
                                 generation_id='gen-a',client_message_id='client-a',callback_identity='callback-a')

    async def test_ack_is_not_completion_and_duplicate_request_is_not_replayed(self):
        self.manager.reserve('a',self.body); self.manager.reserve('a',self.body)
        self.assertEqual(self.calls,[])
        await self.manager.run_once()
        self.assertEqual(self.state(),'unknown')
        self.manager.reserve('a',self.body)
        self.assertFalse(await self.manager.run_once())
        self.assertEqual([c for c in self.calls if c[0]=='thread/compact/start'],[('thread/compact/start',{'threadId':'thread-a'})])
        with self.assertRaises(CompactionError): self.manager.reserve('a',{'request_id':str(uuid.uuid4())})
        with self.assertRaises(CompactionError): self.manager.reserve('b',self.body)

    async def test_pre_ack_completion_is_durable_without_chat_or_private_text(self):
        self.behavior='complete'; self.manager.reserve('a',self.body); await self.manager.run_once()
        self.assertEqual(self.state(),'completed'); self.assertTrue(self.manager.status('a')['can_start'])
        with closing(store.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM codex_generation_jobs').fetchone()[0],0)
            row=dict(conn.execute('SELECT * FROM web_codex_compactions').fetchone())
        self.assertNotIn('never persist',json.dumps(row)); self.assertNotIn('thread-a',json.dumps(self.manager.status('a')))

    async def test_wrong_thread_old_turn_and_non_compaction_cannot_complete(self):
        self.behavior='hold';self.manager.reserve('a',self.body);task=asyncio.create_task(self.manager.run_once())
        while not self.manager.active: await asyncio.sleep(0)
        self.event('item/completed',threadId='thread-b',turnId='new-turn',item={'type':'contextCompaction'})
        self.event('item/completed',turnId='old-turn',item={'type':'contextCompaction'})
        self.event('item/completed',turnId='new-turn',item={'type':'reasoning'})
        self.event('turn/completed',turn={'id':'new-turn','status':'completed'})
        self.assertEqual(self.state(),'dispatching')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.state(),'unknown')

    async def test_cancel_does_not_overwrite_observed_completion(self):
        self.behavior='cancel_after_complete'; self.manager.reserve('a',self.body)
        with self.assertRaises(asyncio.CancelledError): await self.manager.run_once()
        self.assertEqual(self.state(),'completed')

    async def test_transport_failure_never_auto_retries(self):
        self.behavior='error';self.manager.reserve('a',self.body);await self.manager.run_once()
        self.assertEqual(self.state(),'unknown');self.assertFalse(await self.manager.run_once())

    async def test_restart_preserves_schema_and_never_replays_dispatched_work(self):
        self.manager.reserve('a',self.body);self.manager.initialize();self.assertEqual(self.state(),'queued')
        self.manager.update(self.body['request_id'],'dispatching',before_ids='["old-turn"]')
        store.initialize(self.path)
        restarted=Compactions(self.runtime);restarted.initialize()
        self.assertEqual(self.state(),'unknown');self.assertFalse(await restarted.run_once());self.assertEqual(self.calls,[])

    async def test_reconcile_reads_once_and_requires_unique_matching_evidence(self):
        self.manager.reserve('a',self.body);await self.manager.run_once()
        self.page={'data':[{'id':'new-turn','status':'completed','items':[{'type':'contextCompaction'}]}]}
        self.manager.reserve('a',self.body,reconcile=True);await self.manager.run_once()
        self.assertEqual(self.state(),'completed')
        self.assertEqual(len([c for c in self.calls if c[0]=='thread/compact/start']),1)
        with closing(store.connect(self.path)) as conn: row=dict(conn.execute('SELECT * FROM web_codex_compactions').fetchone())
        self.assertEqual(self.manager.reconcile_page(row,{'data':self.page['data']*2}),'unknown')
        self.assertEqual(self.manager.reconcile_page(row,{'data':[{'id':'old-turn','status':'completed','items':[{'type':'contextCompaction'}]}]}),'unknown')

    async def test_admission_and_retirement_block_pending_compaction(self):
        self.manager.reserve('a',self.body)
        with self.assertRaisesRegex(store.CodexGenerationStoreError,'compaction_pending'):self.enqueue()
        with self.assertRaises(store.CodexGenerationStoreError):store.retire_session(self.path,api_session='a')
        self.enqueue('b')
        with self.assertRaises(CompactionError):self.manager.reserve('b',{'request_id':str(uuid.uuid4())})

    async def test_persona_change_fails_before_mutation(self):
        self.manager.reserve('a',self.body);self.persona='changed';await self.manager.run_once()
        self.assertEqual(self.state(),'failed');self.assertEqual(self.calls,[])


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.db=Path(temp.name)/'relay.db'
        with sqlite3.connect(self.db) as conn:conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
    def message(self,text,sid='a',**meta):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES ('2026-10-12T00:00:00Z','in','user',?,?)",(text,json.dumps({'api_session':sid,**meta}))).lastrowid
    def test_sources_settings_isolation_and_attachment_omission(self):
        context.save(self.db,'a',{'revision':0,'config':{**context.defaults(),'project':'visible project','user_name':'disabled name','relationship_enabled':False}})
        mid=self.message('synthetic text',attachments=[{'url':'https://private.invalid/?token=secret'}]);self.message('other conversation','b')
        value=preview(self.db,'a','title');text=value['text']
        for included in ('visible project','synthetic text','附件未带入',f'消息 #{mid}') :self.assertIn(included,text)
        for excluded in ('disabled name','other conversation','private.invalid','secret'):self.assertNotIn(excluded,text)
        self.assertEqual(value['sources'],[{'message_id':mid,'truncated':False}]);self.assertFalse(value['partial'])
    def test_utf16_budget_omission_and_newest_first_selection(self):
        config={**context.defaults(),**{k:'😀'*limit for k,limit in context.TEXT_LIMITS.items() if k!='timezone'}}
        context.save(self.db,'a',{'revision':0,'config':config})
        for i in range(30):self.message(f'older-{i}')
        latest=self.message('😀'*2000)
        value=preview(self.db,'a','😀'*240)
        self.assertLessEqual(len(value['text'].encode('utf-16-le'))//2,12000);self.assertTrue(value['partial'])
        # With normal settings the newest overlong message is visibly truncated.
        context.save(self.db,'a',{'revision':1,'config':context.defaults()})
        value=preview(self.db,'a','title');self.assertEqual(value['sources'][-1],{'message_id':latest,'truncated':True})
        self.assertIn('本条仅摘录前 1000 字',value['text']);self.assertLessEqual(len(value['sources']),24)
    def test_empty_preview_has_no_invented_decisions(self):
        value=preview(self.db,'a','empty');self.assertEqual(value['sources'],[])
        self.assertIn('关键决定：\n尚未解决的问题：\n下一步：',value['text'])


if __name__=='__main__':unittest.main()
