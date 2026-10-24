"""Synthetic persistence, exact aggregation, cleanup and snapshot-page contracts."""
import copy
from contextlib import closing
import csv
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.usage_ledger import Ledger,LedgerError,build_view,summarize,raw_json
from backend.usage_ledger_journal import CollectionJournal,coverage
from backend.usage_ledger_storage import page,maintain,stream_export
from backend.tests.test_usage_ledger import record


class StorageTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.ledger=Ledger(self.root/'ledger.sqlite')
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('no outbound requests')))

    def rows(self,n=5):
        for i in range(n):self.ledger.append(record(request_id='request-'+str(i),turn_id='turn-'+str(i)))

    def test_journal_pending_survives_restart_then_append_commits_it_atomically(self):
        r=record();j=CollectionJournal(self.ledger,'api');j.start(r)
        restarted=Ledger(self.ledger.path)
        self.assertEqual(coverage(restarted,'api','s-1')['unconfirmed'],1)
        restarted.append(r);self.assertEqual(coverage(restarted,'api','s-1')['unconfirmed'],0)
        self.assertEqual(coverage(restarted,'codex','s-1')['observed_runs'],0)

    def test_known_write_failure_is_durable_and_resolves_only_after_record_arrives(self):
        r=record();j=CollectionJournal(self.ledger,'api');j.start(r)
        j.issue('write_failed',session_id='s-1',provider_id='provider-a',request_id='request-1')
        self.assertEqual(coverage(Ledger(self.ledger.path),'api','s-1')['issues'],{'write_failed':1})
        self.ledger.append(r);self.assertEqual(coverage(self.ledger,'api','s-1')['issues'],{})

    def test_issue_enum_identity_and_unassigned_scope_do_not_store_error_text(self):
        j=CollectionJournal(self.ledger,'codex','subscription-a')
        j.issue('pending_evicted',request_id='opaque-native-hash');j.issue('pending_evicted',request_id='opaque-native-hash')
        self.assertEqual(coverage(self.ledger,'codex','s-1')['unassigned_issues'],1)
        self.assertEqual(coverage(self.ledger,'api','s-1')['unassigned_issues'],0)
        with self.assertRaises(LedgerError):j.issue('exception contains private endpoint')

    def test_model_or_session_filter_cannot_hide_other_session_budget_gaps(self):
        self.ledger.append(record());j=CollectionJournal(self.ledger,'api');j.start(record(session_id='s-other',request_id='unconfirmed'))
        self.ledger.save_settings('api',0,{'budgets':[{'period':'day','currency':'USD','limit':'10'}]})
        view=build_view(self.ledger,'api','s-1',model='mock-a',now=datetime(2026,10,23,3,tzinfo=timezone.utc))
        self.assertEqual(view['collection_health']['unconfirmed'],0);self.assertTrue(view['collection_health']['budget_has_observed_gaps'])
        self.assertIsNone(view['budgets'][0]['remaining'])

    def test_cursor_is_signed_bound_to_scope_and_survives_restart(self):
        self.rows();first=page(self.ledger,'api','s-1',limit=2)
        self.assertEqual([r['request_id'] for r in first['records']],['request-4','request-3'])
        second=page(Ledger(self.ledger.path),'api','s-1',limit=2,cursor=first['next_cursor'])
        self.assertEqual([r['request_id'] for r in second['records']],['request-2','request-1'])
        for fields in ({'mode':'codex','sid':'s-1'},{'mode':'api','sid':None},{'mode':'api','sid':'s-1','model':'other'}):
            with self.assertRaises(LedgerError):page(self.ledger,limit=2,cursor=first['next_cursor'],**fields)
        with self.assertRaises(LedgerError):page(self.ledger,'api','s-1',limit=2,cursor=first['next_cursor']+'x')

    def test_new_and_backdated_appends_do_not_shift_existing_snapshot_pages(self):
        self.rows();first=page(self.ledger,'api','s-1',limit=2)
        self.ledger.append(record(request_id='new'));self.ledger.append(record(request_id='backdated',at='2026-10-22T00:00:00Z'))
        cursor=first['next_cursor'];ids=[r['request_id'] for r in first['records']]
        while cursor:
            result=page(self.ledger,'api','s-1',limit=2,cursor=cursor);ids.extend(r['request_id'] for r in result['records']);cursor=result['next_cursor']
        self.assertEqual(len(ids),5);self.assertEqual(len(set(ids)),5);self.assertNotIn('backdated',ids)
        self.assertEqual(len(page(self.ledger,'api','s-1',limit=20)['records']),7)

    def test_raw_expiry_keeps_exact_prices_and_normalized_usage_but_never_restores_raw_on_replay(self):
        r=record();self.ledger.append(r);before=self.ledger.records('api')[0]
        preview=maintain(self.ledger,'api',raw_before='2026-10-24T00:00:00Z',preview=True)
        self.assertEqual(preview['raw_records'],1);self.assertIsNotNone(self.ledger.records('api')[0]['raw_usage'])
        maintain(self.ledger,'api',raw_before='2026-10-24T00:00:00Z')
        after=self.ledger.records('api')[0]
        self.assertIsNone(after['raw_usage']);self.assertEqual(after['normalized'],before['normalized']);self.assertEqual(after['cost'],before['cost'])
        self.assertFalse(self.ledger.append(r));self.assertIsNone(self.ledger.records('api')[0]['raw_usage'])
        exported=list(csv.DictReader(io.StringIO(''.join(stream_export(self.ledger,'api',format='csv')))))
        self.assertEqual(exported[0]['raw_removed'],'True');self.assertEqual(exported[0]['raw_usage_json'],'null')
        with self.assertRaises(LedgerError):self.ledger.append(record(latency_ms=999))

    def test_full_expiry_is_mode_specific_explicit_and_invalidates_stale_cursors(self):
        self.rows();first=page(self.ledger,'api','s-1',limit=2)
        self.ledger.append(record(mode='codex',provider_id='subscription',dialect='codex',raw_usage=None,price=None))
        maintain(self.ledger,'api',records_before='2026-10-24T00:00:00Z')
        self.assertEqual(self.ledger.records('api'),[]);self.assertEqual(len(self.ledger.records('codex')),1)
        self.assertFalse(self.ledger.append(record()))
        with self.assertRaises(LedgerError):page(self.ledger,'api','s-1',limit=2,cursor=first['next_cursor'])
        self.assertTrue(build_view(self.ledger,'api','s-1')['collection_health']['retention']['records_removed'])
        with self.assertRaisesRegex(LedgerError,'cannot_restore'):maintain(self.ledger,'api',records_before='2026-10-22T00:00:00Z')

    def test_late_backfill_obeys_existing_raw_retention_on_first_insert(self):
        maintain(self.ledger,'api',raw_before='2026-10-24T00:00:00Z')
        r=record(request_id='late-backfill',at='2026-10-22T00:00:00Z')
        self.assertTrue(self.ledger.append(r));saved=self.ledger.records('api')[0]
        self.assertIsNone(saved['raw_usage']);self.assertTrue(saved['_retention']['raw_removed'])
        self.assertEqual(saved['normalized']['metrics']['input'],1000);self.assertIsNotNone(saved['cost']['amount'])
        self.assertFalse(self.ledger.append(r));self.assertIsNone(self.ledger.records('api')[0]['raw_usage'])

    def test_retired_session_rejects_new_and_late_records_events_and_journal(self):
        self.rows();self.ledger.purge_session('s-1',retire=True,mode='api')
        r=record(request_id='new-late',at=datetime.now(timezone.utc).isoformat());self.assertFalse(self.ledger.append(r))
        j=CollectionJournal(self.ledger,'api');j.start(r);j.issue('write_failed',session_id='s-1')
        self.assertEqual(coverage(self.ledger,'api','s-1')['unconfirmed'],0)
        self.assertFalse(self.ledger.event({'event_id':'late','session_id':'s-1','mode':'api','kind':'compaction','status':'unknown','at':r['at']}))
        self.assertEqual(self.ledger.records('api'),[]);self.assertEqual(self.ledger.events('api'),[])
        self.assertEqual(coverage(self.ledger,'codex')['retention']['retired_sessions'],0)

    def test_journal_start_and_real_retirement_serialize_without_recreating_pending_rows(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        journal=CollectionJournal(self.ledger,'api')
        for i in range(4):
            sid='race-'+str(i);r=record(session_id=sid,request_id='race-request-'+str(i));barrier=Barrier(2)
            def start():barrier.wait();journal.start(r)
            def retire():barrier.wait();self.ledger.purge_session(sid,retire=True,mode='api')
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(start);b=pool.submit(retire);a.result();b.result()
            self.assertEqual(coverage(self.ledger,'api',sid)['unconfirmed'],0)

    def test_stream_export_is_consistent_with_appends_and_rejects_cleanup_race(self):
        self.rows(3);chunks=stream_export(self.ledger,'api','s-1');head=next(chunks)
        self.ledger.append(record(request_id='after-snapshot'))
        data=json.loads(head+''.join(chunks));self.assertEqual(len(data['records']),3)
        csv_rows=list(csv.DictReader(io.StringIO(''.join(stream_export(self.ledger,'api','s-1',format='csv')))))
        self.assertEqual(len(csv_rows),4);self.assertIn('raw_usage_json',csv_rows[0])
        changed=stream_export(self.ledger,'api','s-1');next(changed);self.ledger.purge_session('s-1')
        with self.assertRaisesRegex(LedgerError,'export_changed'):list(changed)

    def test_streaming_summary_matches_existing_decimal_and_coverage_contract(self):
        self.rows(3);self.ledger.append(record(request_id='missing',raw_usage=None,price=None,usage_complete=False))
        expected=summarize(self.ledger.records('api'));actual=build_view(self.ledger,'api','s-1')['summary']
        self.assertEqual(actual,expected)

    def test_more_than_fifty_thousand_records_have_exact_summary_and_streamed_export(self):
        self.ledger.append(record());payload=self.ledger.records('api')[0];self.ledger.purge_session('s-1')
        with closing(self.ledger.connect()) as conn,conn:
            conn.execute('''WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<50001)
                INSERT INTO usage_ledger SELECT 'api','provider-a','bulk-'||x,'s-1',?,json_set(?, '$.request_id','bulk-'||x,'$.turn_id','turn-'||x) FROM n''',(payload['at'],raw_json(payload)))
        ledger=Ledger(self.ledger.path)
        view=build_view(ledger,'api','s-1');self.assertEqual(view['summary']['requests'],50001);self.assertEqual(view['summary']['metrics']['input']['value'],50001000)
        self.assertEqual(len(page(ledger,'api','s-1')['records']),25)
        self.assertEqual(sum(1 for _ in stream_export(ledger,'api','s-1')),50003)


class StorageRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_paged_routes_auth_validation_scope_and_streaming_export(self):
        import httpx
        from fastapi import FastAPI,HTTPException
        from backend.usage_ledger_routes import install
        with tempfile.TemporaryDirectory() as temp:
            ledger=Ledger(Path(temp)/'ledger.sqlite');ledger.append(record());app=FastAPI()
            def auth(request):
                if request.headers.get('authorization')!='Bearer synthetic':raise HTTPException(401)
            install(app,ledger=ledger,check_auth=auth,session_provider=lambda sid:'api' if sid=='s-1' else None)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
                url='/app/sessions/s-1/usage-ledger/records'
                self.assertEqual((await client.get(url)).status_code,401);client.headers['Authorization']='Bearer synthetic'
                response=await client.get(url);self.assertEqual(response.status_code,200);self.assertEqual(len(response.json()['records']),1);self.assertEqual(response.headers['cache-control'],'private, no-store')
                for query in ('?limit=0','?limit=201','?limit=-1','?limit=2&limit=3','?cursor=forged'):
                    self.assertEqual((await client.get(url+query)).status_code,400)
                self.assertEqual((await client.get(url+'?mode=codex')).json()['records'],[])
                self.assertEqual(len((await client.get('/app/sessions/s-1/usage-ledger/export')).json()['records']),1)

    async def test_real_conversation_delete_opt_in_cleans_ledger_and_blocks_delayed_callback(self):
        from backend.web_session_provider_authority import WebSessionProviderAuthority
        from backend.web_session_delete import delete_conversation
        with tempfile.TemporaryDirectory() as temp:
            relay=Path(temp)/'relay.sqlite';ledger=Ledger(Path(temp)/'ledger.sqlite');ledger.append(record())
            with sqlite3.connect(relay) as conn:
                conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
                conn.execute('INSERT INTO messages VALUES (1,?,?,?,?,?)',('2026-10-23T02:00:00Z','in','user','synthetic deleted text',json.dumps({'api_session':'s-1'})))
            cfg={'sessions':[{'id':sid,'provider':'api','title':'synthetic'} for sid in ('s-1','other')]}
            legacy=SimpleNamespace(load_config=lambda:copy.deepcopy(cfg),save_config=lambda value:cfg.update(copy.deepcopy(value)),now_iso=lambda:datetime.now(timezone.utc).isoformat())
            authority=WebSessionProviderAuthority(legacy)
            result=delete_conversation(authority,'s-1',relay_db=relay,upload_dir=None,codex_store=None,usage_ledger=ledger)
            self.assertTrue(result['deleted']['ledger_deleted']);self.assertEqual(ledger.records('api'),[])
            self.assertFalse(ledger.append(record(request_id='new-callback',at=datetime.now(timezone.utc).isoformat())))
            again=delete_conversation(authority,'s-1',relay_db=relay,upload_dir=None,codex_store=None,usage_ledger=ledger)
            self.assertTrue(again['deleted']['duplicate'])


if __name__=='__main__':unittest.main()
