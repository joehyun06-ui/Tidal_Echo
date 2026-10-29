"""Real server factory/authority with a mocked native pin, never a real SDK."""
import asyncio
import copy
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from backend.usage_ledger import Ledger
from backend.usage_ledger_continuation import Continuations
from backend.usage_ledger_creation import ContinuationCreation,CreationError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegration


class CreationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.relay=self.root/'relay.sqlite';self.ledger=Ledger(self.root/'ledger.sqlite')
        self.cfg={'sessions':[{'id':sid,'title':'synthetic source','provider':mode,'since_id':0,'created_at':''} for sid,mode in [('a','api'),('b','codex')]]}
        self.legacy=SimpleNamespace(load_config=lambda:copy.deepcopy(self.cfg),save_config=lambda value:self.cfg.update(copy.deepcopy(value)),now_iso=lambda:datetime.now(timezone.utc).isoformat(),RELAY_DB=self.relay)
        self.authority=WebSessionProviderAuthority(self.legacy);self.lock=RLock()
        self.integration=object.__new__(CodexCanaryLoopIntegration)
        self.integration.session_authority=self.authority;self.integration._session_lock=self.lock
        self.controller=SimpleNamespace(pin_session=AsyncMock(),retire_session=Mock())
        self.integration.runtime=SimpleNamespace(generation_enabled=True,controller=self.controller)
        with sqlite3.connect(self.relay) as conn:
            conn.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT);
                CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY,root_id TEXT,parent_id TEXT,mode TEXT);
                CREATE TABLE web_version_selections(root_id TEXT PRIMARY KEY,version_id TEXT);''')
        self.receipts=Continuations(self.ledger,relay_db=self.relay,session_row=self.authority.row_for_session,digest_key=b'synthetic-creation-key-for-offline-only')
        self.creator=self.make();self.body={'request_id':'11111111-1111-4111-8111-111111111111','source_version_id':'a','text':'synthetic edited continuation'}
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('network forbidden')))

    def make(self,factory=None):return ContinuationCreation(self.receipts,authority=self.authority,session_lock=self.lock,create_session=factory or self.integration.create_web_session)

    async def test_api_and_codex_use_real_factory_target_and_register_without_sending(self):
        for sid,mode in [('a','api'),('b','codex')]:
            body={**self.body,'source_version_id':sid,'request_id':self.body['request_id'] if sid=='a' else '22222222-2222-4222-8222-222222222222'}
            result=await self.creator.create(sid,body)
            self.assertTrue(result['registered']);self.assertEqual(result['created']['provider'],mode)
            self.assertTrue(result['created']['id'].startswith(mode+'-continuation-'))
            self.receipts.reconcile(sid,mode)
            self.assertEqual([e['snapshot']['stage'] for e in self.ledger.events(mode)],['target_created'])
        self.assertEqual(self.controller.pin_session.await_count,1)
        with sqlite3.connect(self.relay) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)
        self.assertNotIn(self.body['text'].encode(),Path(self.ledger.path).read_bytes())

    async def test_duplicate_concurrent_requests_create_one_target(self):
        factory=AsyncMock(wraps=self.integration.create_web_session);creator=self.make(factory)
        results=await asyncio.gather(*(creator.create('a',self.body) for _ in range(4)))
        self.assertEqual(len({r['created']['id'] for r in results}),1);self.assertEqual(factory.await_count,1)
        self.assertEqual(len(self.authority.session_rows()),3)
        with self.assertRaisesRegex(CreationError,'conflict'):await creator.create('a',{**self.body,'text':'different edited package'})

    async def test_unknown_factory_outcome_never_repeats_and_can_reconcile_exact_target(self):
        async def failed(**kwargs):
            await self.integration.create_web_session(**kwargs)
            raise OSError('synthetic lost factory result')
        factory=AsyncMock(side_effect=failed);creator=self.make(factory)
        with self.assertRaisesRegex(CreationError,'uncertain'):await creator.create('a',self.body)
        restarted=self.make(factory)
        result=await restarted.create('a',self.body)
        self.assertTrue(result['registered']);self.assertEqual(factory.await_count,1)

    async def test_unknown_without_target_is_not_replayed(self):
        factory=AsyncMock(side_effect=OSError('synthetic before result'));creator=self.make(factory)
        for _ in range(2):
            with self.assertRaisesRegex(CreationError,'uncertain'):await creator.create('a',self.body)
        self.assertEqual(factory.await_count,1)

    async def test_recovery_after_restart_only_registers_exact_existing_target(self):
        for sid in ('a','b'):
            body={**self.body,'source_version_id':sid,'request_id':self.body['request_id'] if sid=='a' else '22222222-2222-4222-8222-222222222222'}
            async def lost(**kwargs):
                await self.integration.create_web_session(**kwargs)
                raise OSError('synthetic lost factory result')
            factory=AsyncMock(side_effect=lost)
            with self.assertRaisesRegex(CreationError,'uncertain'):await self.make(factory).create(sid,body)
            forbidden=AsyncMock(side_effect=AssertionError('recovery must not create'))
            restarted=self.make(forbidden)
            results=await asyncio.gather(*(restarted.reconcile(sid,body) for _ in range(3)))
            self.assertEqual(len({r['created']['id'] for r in results}),1)
            self.assertTrue(all(r['registered'] for r in results));forbidden.assert_not_awaited();self.assertEqual(factory.await_count,1)
        with sqlite3.connect(self.relay) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)
        with closing(self.ledger.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_ledger_continuations').fetchone()[0],2)
        self.assertNotIn(self.body['text'].encode(),Path(self.ledger.path).read_bytes())

    async def test_recovery_without_saved_request_never_reserves_or_calls_factory(self):
        factory=AsyncMock(side_effect=AssertionError('must not create'));creator=self.make(factory)
        with self.assertRaisesRegex(CreationError,'receipt_unavailable'):await creator.reconcile('a',self.body)
        with closing(self.ledger.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_ledger_continuation_creations').fetchone()[0],0)
        factory.assert_not_awaited()

    async def test_crashed_creation_with_no_target_remains_unknown_on_recovery(self):
        factory=AsyncMock(side_effect=OSError('synthetic before result'));creator=self.make(factory)
        with self.assertRaises(CreationError):await creator.create('a',self.body)
        with closing(self.ledger.connect()) as conn,conn:conn.execute("UPDATE usage_ledger_continuation_creations SET phase='creating'")
        forbidden=AsyncMock(side_effect=AssertionError('must not recreate'))
        for _ in range(2):
            with self.assertRaisesRegex(CreationError,'uncertain'):await self.make(forbidden).reconcile('a',self.body)
        forbidden.assert_not_awaited();self.assertEqual(factory.await_count,1)

    async def test_recovery_rejects_changed_text_source_selection_and_purged_receipt(self):
        await self.creator.create('a',self.body)
        for sid,body in [('a',{**self.body,'text':'changed'}),('b',{**self.body,'source_version_id':'b'})]:
            with self.assertRaisesRegex(CreationError,'conflict'):await self.creator.reconcile(sid,body)
        self.ledger.purge_session('a')
        with self.assertRaisesRegex(CreationError,'receipt_unavailable'):await self.creator.reconcile('a',self.body)

    async def test_recovery_routes_are_authenticated_bounded_and_never_retry(self):
        import httpx
        from fastapi import FastAPI,HTTPException
        from backend.web_context_actions_routes import install_loop,install_relay
        def auth(request):
            if request.headers.get('authorization')!='Bearer synthetic':raise HTTPException(401)
        self.legacy.check_internal_auth=auth
        await self.creator.create('a',self.body)
        app=FastAPI();install_loop(app,self.legacy,self.integration,self.integration.runtime,continuation_creator=self.creator)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
            url='/loop/sessions/a/continuation/reconcile'
            self.assertEqual((await client.post(url,json=self.body)).status_code,401)
            client.headers['Authorization']='Bearer synthetic'
            result=await client.post(url,json=self.body);self.assertEqual(result.status_code,200);self.assertTrue(result.json()['registered'])
            self.assertEqual((await client.post(url,content=b'x'*(160*1024+1))).status_code,400)
        calls=[]
        def forward(path,**kw):calls.append((path,kw));raise HTTPException(503)
        app=FastAPI();install_relay(SimpleNamespace(app=app,check_auth=auth,loop_json=forward))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
            url='/app/sessions/a/continuation/reconcile'
            self.assertEqual((await client.post(url,json=self.body)).status_code,401);self.assertEqual(calls,[])
            client.headers['Authorization']='Bearer synthetic'
            result=await client.post(url,json=self.body);self.assertEqual(result.status_code,503);self.assertEqual(result.headers['cache-control'],'private, no-store')
            self.assertEqual(calls,[('/loop/sessions/a/continuation/reconcile',{'method':'POST','body':self.body})])

    async def test_recovery_requires_unchanged_selection_key_and_factory_marker(self):
        await self.creator.create('a',self.body)
        row=self.authority.new_row(provider='api',session_id='alt');self.authority.publish_row(row,activate=False)
        with sqlite3.connect(self.relay) as conn:
            conn.execute("INSERT INTO web_message_versions VALUES ('alt','a','a','regenerate')")
            conn.execute("INSERT INTO web_version_selections VALUES ('a','alt')")
        with self.assertRaisesRegex(CreationError,'selection_changed'):await self.creator.reconcile('a',self.body)
        with sqlite3.connect(self.relay) as conn:conn.execute("DELETE FROM web_version_selections")
        with closing(self.ledger.connect()) as conn,conn:conn.execute("UPDATE usage_ledger_continuation_creations SET phase='reserved'")
        with self.assertRaisesRegex(CreationError,'uncertain'):await self.creator.reconcile('a',self.body)
        with closing(self.ledger.connect()) as conn,conn:conn.execute("UPDATE usage_ledger_continuation_creations SET phase='unknown',key_id='synthetic-other-key'")
        with self.assertRaisesRegex(CreationError,'conflict'):await self.creator.reconcile('a',self.body)

    async def test_cross_source_or_extra_fields_and_bad_utf16_are_rejected(self):
        for change in ({'provider':'codex'},{'target_id':'client-chosen'},{'text':' x '},{'text':'\ud800'},{'source_version_id':'b'}):
            with self.assertRaises(CreationError):await self.creator.create('a',{**self.body,**change})
        await self.creator.create('a',self.body)
        with self.assertRaisesRegex(CreationError,'conflict'):await self.creator.create('b',{**self.body,'source_version_id':'b'})
        self.assertEqual(self.controller.pin_session.await_count,0)

    async def test_selected_version_preview_and_stale_selection_prevent_creation(self):
        row=self.authority.new_row(provider='api',session_id='alt');self.authority.publish_row(row,activate=False)
        with sqlite3.connect(self.relay) as conn:
            conn.execute("INSERT INTO web_message_versions VALUES ('alt','a','a','regenerate')")
            conn.execute("INSERT INTO web_version_selections VALUES ('a','alt')")
            conn.execute('INSERT INTO messages VALUES (1,?,?,?,?,?)',('2026-10-24T00:00:00Z','in','user','selected synthetic text',json.dumps({'api_session':'alt'})))
        value=self.creator.preview('a');self.assertIn('selected synthetic text',value['text'])
        self.assertEqual(value['registration']['source_version_id'],'alt')
        with self.assertRaisesRegex(CreationError,'selection_changed'):await self.creator.create('a',self.body)

    async def test_deleted_target_and_purge_during_creation_do_not_register_or_recreate(self):
        async def purged(**kwargs):
            await self.integration.create_web_session(**kwargs)
            self.ledger.purge_session('a')
        creator=self.make(purged)
        with self.assertRaisesRegex(CreationError,'registration_unconfirmed'):await creator.create('a',self.body)
        self.assertEqual(self.ledger.events('api'),[])
        with closing(self.ledger.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_ledger_continuations').fetchone()[0],0)
        with self.assertRaisesRegex(CreationError,'target_conflict'):await creator.create('a',self.body)

    async def test_real_loop_routes_auth_body_bound_and_default_off(self):
        import httpx
        from fastapi import FastAPI,HTTPException
        from backend.web_context_actions_routes import install_loop
        def auth(request):
            if request.headers.get('authorization')!='Bearer synthetic':raise HTTPException(401)
        self.legacy.check_internal_auth=auth
        for creator in (None,self.creator):
            app=FastAPI();install_loop(app,self.legacy,self.integration,self.integration.runtime,continuation_creator=creator)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
                url='/loop/sessions/a/continuation'
                self.assertEqual((await client.post(url,json=self.body)).status_code,401)
                client.headers['Authorization']='Bearer synthetic'
                response=await client.post(url,json=self.body)
                self.assertEqual(response.status_code,409 if creator is None else 200)
                preview=await client.get(url)
                self.assertEqual(preview.json().get('registration',{}).get('supported'),None if creator is None else True)
                if creator:
                    self.assertEqual((await client.post(url,content=b'x'*(160*1024+1))).status_code,400)

    async def test_real_relay_forwarding_auth_limits_and_unknown_reply_never_retry(self):
        import httpx
        from fastapi import FastAPI,HTTPException
        from backend.web_context_actions_routes import install_relay
        app=FastAPI();calls=[];fail=False
        def auth(request):
            if request.headers.get('authorization')!='Bearer synthetic':raise HTTPException(401)
        def forward(path,**kw):
            calls.append((path,kw))
            if fail:raise HTTPException(503)
            return {'ok':True,'api_session':'a','registered':True}
        install_relay(SimpleNamespace(app=app,check_auth=auth,loop_json=forward))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
            url='/app/sessions/a/continuation'
            self.assertEqual((await client.post(url,json=self.body)).status_code,401);self.assertEqual(calls,[])
            client.headers['Authorization']='Bearer synthetic'
            result=await client.post(url,json=self.body);self.assertEqual(result.status_code,200);self.assertEqual(result.headers['cache-control'],'private, no-store')
            self.assertEqual(calls,[('/loop/sessions/a/continuation',{'method':'POST','body':self.body})])
            self.assertEqual((await client.post(url,content=b'x'*(160*1024+1))).status_code,400);self.assertEqual(len(calls),1)
            fail=True;self.assertEqual((await client.post(url,json=self.body)).status_code,503);self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
