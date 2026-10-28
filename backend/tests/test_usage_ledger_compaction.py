"""Manual compaction receipts through the real manager and synthetic transport."""
import asyncio
from contextlib import closing
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from backend.usage_ledger import Ledger,build_view
from backend.usage_ledger_compaction import ManualCompactionReceipts
from backend.web_codex_compaction import Compactions
from backend.codex_generation_protocol import CodexProcessActivityGate


class ManualReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.native=self.root/'native.sqlite';self.relay=self.root/'relay.sqlite';self.ledger=Ledger(self.root/'ledger.sqlite')
        self.rows={'a':{'id':'a','provider':'codex'}};self.calls=[];self.behavior='complete';self.post_usage=True
        self.persona='synthetic persona'
        with sqlite3.connect(self.native) as conn:
            conn.executescript('''CREATE TABLE codex_sessions(api_session TEXT PRIMARY KEY,status TEXT,thread_id TEXT,model TEXT,model_provider TEXT,reasoning_effort TEXT,cwd TEXT,persona_hash TEXT);
                CREATE TABLE codex_generation_jobs(id INTEGER PRIMARY KEY,api_session TEXT,canonical_message_id INTEGER,thread_id TEXT,status TEXT);
                INSERT INTO codex_generation_jobs VALUES (1,'a',1,'thread-a','completed');''')
            conn.execute('INSERT INTO codex_sessions VALUES (?,?,?,?,?,?,?,?)',('a','active','thread-a','synthetic-model','openai',None,str(self.root),hashlib.sha256(self.persona.encode()).hexdigest()))
        with sqlite3.connect(self.relay) as conn:
            conn.executescript('CREATE TABLE messages(id INTEGER PRIMARY KEY,direction TEXT,kind TEXT,meta TEXT);CREATE TABLE web_context_observations(session_id TEXT PRIMARY KEY,payload TEXT);')
            conn.execute('INSERT INTO messages VALUES (1,?,?,?)',('in','user',json.dumps({'api_session':'a'})))
            conn.execute('INSERT INTO web_context_observations VALUES (?,?)',('a',json.dumps({'canonical_message_id':1,'context_tokens':180000,'observed_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})))
        self.collector=ManualCompactionReceipts(self.ledger,native_db=self.native,relay_db=self.relay,session_row=self.rows.get)
        async def resume(**kwargs):return {'data':[]}
        async def request(method,params):
            self.calls.append(method)
            if self.behavior=='complete':
                self.manager.receive('turn/started',{'threadId':'thread-a','turn':{'id':'compact-turn','status':'inProgress'}})
                self.manager.receive('item/completed',{'threadId':'thread-a','turnId':'compact-turn','item':{'type':'contextCompaction'}})
                if self.post_usage:self.sample()
                self.manager.receive('turn/completed',{'threadId':'thread-a','turn':{'id':'compact-turn','status':'completed'}})
            return {}
        def scope(**kw):self.notifications=kw['notifications'];return SimpleNamespace(request=request)
        self.runtime=SimpleNamespace(config=SimpleNamespace(store_path=self.native),relay_db=self.relay,persona_loader=lambda:self.persona,
            foundation=SimpleNamespace(runtime=SimpleNamespace(scope=scope),generation=SimpleNamespace(resume_thread=resume),activity_gate=CodexProcessActivityGate()))
        self.manager=Compactions(self.runtime,timeout=.001,usage_collector=self.collector);self.manager.initialize()
        self.rid=str(uuid.uuid4())
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('no outbound networking')))

    def sample(self,turn='compact-turn',thread='thread-a'):
        self.manager.receive('thread/tokenUsage/updated',{'threadId':thread,'turnId':turn,'tokenUsage':{'last':{'totalTokens':6000},'total':{'totalTokens':990000},'modelContextWindow':200000}})

    async def run_job(self):
        self.manager.reserve('a',{'request_id':self.rid});await self.manager.run_once()
        return self.ledger.events('codex','a')

    async def test_real_manager_receipts_and_exact_turn_usage_not_cumulative_spending(self):
        events=await self.run_job();completed=[e for e in events if e['status']=='completed'][-1]
        self.assertEqual(completed['before_tokens'],180000);self.assertEqual(completed['after_tokens'],6000)
        self.assertIsNone(completed['memory_preserved']);self.assertEqual(self.ledger.records('codex'),[])
        self.assertEqual(self.calls,['thread/compact/start'])
        self.assertIn('thread/tokenUsage/updated',self.notifications)
        count=len(events);self.collector.reconcile('a','codex');self.assertEqual(len(self.ledger.events('codex')),count)

    async def test_ack_unknown_and_missing_post_usage_stay_unknown(self):
        self.behavior='ack';events=await self.run_job()
        self.assertEqual(events[-1]['status'],'unknown');self.assertIsNone(events[-1]['after_tokens'])
        self.manager.reserve('a',{'request_id':self.rid});self.assertFalse(await self.manager.run_once())
        self.assertEqual(self.calls,['thread/compact/start'])

    async def test_late_post_usage_adds_observation_without_rewriting_terminal_snapshot(self):
        self.post_usage=False;events=await self.run_job();original=[e for e in events if e['status']=='completed'][-1]
        self.assertIsNone(original['after_tokens'])
        self.sample();late=self.ledger.events('codex')[-1]
        self.assertEqual(late['after_tokens'],6000);self.assertTrue(late['snapshot']['after_usage_observed'])
        self.assertIsNone(next(e for e in self.ledger.events('codex') if e['event_id']==original['event_id'])['after_tokens'])

    async def test_wrong_thread_turn_stale_before_and_missing_fields_do_not_invent_tokens(self):
        with sqlite3.connect(self.relay) as conn:conn.execute('DELETE FROM web_context_observations')
        self.post_usage=False;await self.run_job();self.sample(turn='unrelated');self.sample(thread='wrong')
        row=self.ledger.events('codex')[-1];self.assertIsNone(row['before_tokens']);self.assertIsNone(row['after_tokens'])

    async def test_queued_observation_does_not_invalidate_current_context(self):
        at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.ledger.event({'event_id':'before','session_id':'a','mode':'codex','kind':'context','status':'observed','at':at,
                           'snapshot':{'tokens':180000,'limit':200000,'stale':False,'observed_at':at,'source':'codex_app_server'}})
        self.manager.reserve('a',{'request_id':self.rid})
        self.assertEqual(build_view(self.ledger,'codex','a')['context']['tokens'],180000)
        await self.manager.run_once()
        self.assertIsNone(build_view(self.ledger,'codex','a')['context']['tokens'])

    async def test_purged_receipt_and_late_notification_cannot_resurrect_from_native_history(self):
        await self.run_job();self.ledger.purge_session('a');self.sample();self.collector.reconcile('a','codex')
        self.assertEqual(self.ledger.events('codex'),[])

    async def test_deleted_or_cross_mode_authority_is_ignored(self):
        await self.run_job();count=len(self.ledger.events('codex'));self.rows['a']['provider']='api'
        self.sample();self.collector.reconcile('a','codex');self.assertEqual(len(self.ledger.events('codex')),count)

    async def test_accounting_failure_cannot_change_or_replay_compaction(self):
        with patch.object(self.collector,'observe',side_effect=OSError('synthetic ledger failure')):
            await self.run_job()
        self.assertEqual(self.manager.status('a')['operation']['status'],'completed');self.assertEqual(self.calls,['thread/compact/start'])


if __name__=='__main__':unittest.main()
