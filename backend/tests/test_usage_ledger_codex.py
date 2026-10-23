"""Synthetic native events and disposable SQLite only; no SDK, memory or network."""
import copy
import csv
import io
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.usage_ledger import Ledger, LedgerError, build_view, export_records, normalize
from backend.usage_ledger_codex import CodexCollector


def counters(inp, out, reasoning=0):
    return {'inputTokens':inp,'outputTokens':out,'reasoningOutputTokens':reasoning,
            'cachedInputTokens':0,'totalTokens':inp+out}


def usage(inp=100, out=20, reasoning=5, *, last=None, limit=200000):
    return {'total':counters(inp,out,reasoning),'last':last or counters(inp,out,reasoning),
            'modelContextWindow':limit,'synthetic_extension':{'counter':7}}


class CodexLedgerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);self.relay=root/'relay.sqlite';self.native=root/'native.sqlite'
        self.ledger=Ledger(root/'ledger.sqlite')
        with sqlite3.connect(self.relay) as c:
            c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,direction TEXT,kind TEXT,meta TEXT,text TEXT)')
        with sqlite3.connect(self.native) as c:
            c.execute('CREATE TABLE codex_sessions(api_session TEXT PRIMARY KEY,thread_id TEXT,model TEXT)')
            c.execute('CREATE TABLE codex_generation_jobs(id INTEGER PRIMARY KEY,api_session TEXT,canonical_message_id INTEGER,thread_id TEXT,turn_id TEXT,status TEXT)')
        self.collector=self.make()
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('offline only')))
        self.enterContext(patch('socket.getaddrinfo',side_effect=AssertionError('offline only')))
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')))

    def make(self, provider='subscription-a', **kw):
        kw.setdefault('exclusive_threads',True)
        return CodexCollector(self.ledger,relay_db=self.relay,store_path=self.native,provider_id=provider,**kw)

    def job(self,n,*,sid='session-a',thread='thread-a',turn=None,status='in_progress',model='synthetic-model'):
        turn=turn or 'turn-'+str(n)
        with sqlite3.connect(self.native) as c:
            c.execute('INSERT OR REPLACE INTO codex_sessions VALUES (?,?,?)',(sid,thread,model))
            c.execute('INSERT INTO codex_generation_jobs VALUES (?,?,?,?,?,?)',(n,sid,n,thread,turn,status))
        with sqlite3.connect(self.relay) as c:
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(n,'in','user',json.dumps({'api_session':sid}),'synthetic private chat text'))
        return turn

    def event(self,method,n,*,thread='thread-a',collector=None,epoch=1,**kw):
        params={'threadId':thread}
        if method in {'turn/started','turn/completed'}:params['turn']={'id':'turn-'+str(n),'status':'completed',**kw}
        else:params.update(turnId='turn-'+str(n),**kw)
        (collector or self.collector).receive(method,params,epoch=epoch)

    def sample(self,n,raw=None,**kw):self.event('thread/tokenUsage/updated',n,tokenUsage=raw or usage(),**kw)
    def flush(self,*,collector=None,epoch=1):(collector or self.collector).reconcile(epoch=epoch)

    def whole(self,n,raw=None,*,epoch=1,collector=None,thread='thread-a',sid='session-a',status='completed'):
        self.job(n,sid=sid,thread=thread)
        self.event('turn/started',n,collector=collector,epoch=epoch,thread=thread)
        self.sample(n,raw,collector=collector,epoch=epoch,thread=thread)
        self.event('turn/completed',n,collector=collector,epoch=epoch,thread=thread,status=status)
        self.flush(collector=collector,epoch=epoch)
        return self.ledger.records('codex',sid)[-1]

    def test_first_turn_without_baseline_retains_raw_and_context_without_fabricating_usage(self):
        raw=usage();row=self.whole(1,raw)
        self.assertEqual(row['raw_usage'],raw);self.assertIsNone(row['usage_baseline'])
        self.assertEqual(row['accounting_basis'],'unavailable');self.assertFalse(row['usage_complete'])
        self.assertTrue(all(v is None for v in row['normalized']['metrics'].values()))
        view=build_view(self.ledger,'codex','session-a')
        self.assertEqual(view['context']['tokens'],120);self.assertEqual(view['context']['limit'],200000)
        self.assertEqual(view['coverage'],'recorded_turns_only');self.assertIsNone(view['summary']['metrics']['input']['value'])
        self.assertEqual(view['summary']['costs'],[]);self.assertIsNone(row['retry_count'])

    def test_unverified_thread_ownership_never_attributes_cross_turn_totals(self):
        collector=self.make(exclusive_threads=False)
        self.whole(1,collector=collector)
        row=self.whole(2,usage(300,60,15),collector=collector)
        self.assertIsNone(row['usage_baseline']);self.assertIsNone(row['normalized']['metrics']['input'])
        self.assertFalse(row['usage_complete']);self.assertEqual(row['raw_usage']['total']['inputTokens'],300)

    def test_multirequest_turn_uses_thread_delta_and_never_sums_duplicate_last_snapshots(self):
        first=usage();self.whole(1,first)
        self.job(2);self.event('turn/started',2)
        intermediate=usage(300,60,15,last=counters(200,40,10))
        final=usage(450,90,20,last=counters(150,30,5))
        for raw in (intermediate,intermediate,final,final):self.sample(2,raw)
        self.event('turn/completed',2);self.flush();self.flush()
        rows=self.ledger.records('codex');self.assertEqual(len(rows),2);row=rows[-1]
        self.assertEqual(row['raw_usage'],final);self.assertEqual(row['usage_baseline'],first['total'])
        self.assertEqual(row['normalized']['metrics']['input'],350);self.assertEqual(row['normalized']['metrics']['output'],70)
        self.assertEqual(row['normalized']['metrics']['reasoning'],15);self.assertTrue(row['usage_complete'])
        view=build_view(self.ledger,'codex','session-a');self.assertEqual(view['last_round']['metrics']['total']['value'],420)
        self.assertEqual(view['context']['tokens'],180);self.assertFalse(view['summary']['metrics']['input']['complete'])
        self.assertIsNone(row['normalized']['metrics']['cache_read']);self.assertIsNone(row['cost']['amount'])

    def test_events_before_ack_are_buffered_until_exact_job_is_persisted(self):
        self.event('turn/started',1);self.sample(1);self.event('turn/completed',1)
        self.flush();self.assertEqual(self.ledger.records('codex'),[])
        self.job(1,turn='different-turn');self.flush();self.assertEqual(self.ledger.records('codex'),[])
        with sqlite3.connect(self.native) as c:c.execute("UPDATE codex_generation_jobs SET turn_id='turn-1'")
        self.flush();self.assertEqual(len(self.ledger.records('codex')),1)
        self.assertEqual(self.collector.health()['captured'],1)

    def test_raw_snapshots_can_update_context_before_terminal_without_creating_usage_rows(self):
        self.job(1);self.event('turn/started',1);self.sample(1);self.flush();self.flush()
        self.assertEqual(self.ledger.records('codex'),[])
        self.assertEqual(build_view(self.ledger,'codex','session-a')['context']['tokens'],120)
        self.event('turn/completed',1);self.flush();self.assertEqual(len(self.ledger.records('codex')),1)

    def test_next_turn_start_invalidates_old_context_before_new_usage_exists(self):
        self.whole(1);self.job(2);self.event('turn/started',2);self.flush()
        view=build_view(self.ledger,'codex','session-a')
        self.assertTrue(view['context']['stale']);self.assertIsNone(view['context']['tokens'])
        self.assertEqual(len(self.ledger.records('codex')),1)

    def test_missing_start_or_usage_remains_incomplete_and_cannot_seed_next_turn(self):
        self.job(1);self.sample(1);self.event('turn/completed',1);self.flush()
        row=self.whole(2,usage(200,40,10));self.assertFalse(row['usage_complete']);self.assertIsNone(row['usage_baseline'])
        self.job(3);self.event('turn/started',3);self.event('turn/completed',3);self.flush()
        row=self.ledger.records('codex')[-1];self.assertIsNone(row['raw_usage']);self.assertFalse(row['usage_complete'])

    def test_counter_regression_or_inconsistent_total_never_becomes_negative_or_zero(self):
        self.whole(1)
        self.job(2);self.event('turn/started',2);self.sample(2,usage(300,60,15));self.sample(2,usage(200,40,10))
        self.event('turn/completed',2);self.flush()
        row=self.ledger.records('codex')[-1];self.assertIsNone(row['usage_baseline']);self.assertFalse(row['usage_complete'])
        row=self.whole(3,usage(400,80,20));self.assertIsNone(row['usage_baseline'])
        u=normalize(usage(1,1,0),'codex',usage_baseline=counters(100,20,5));self.assertIn('codex_counter_reset',u['issues'])
        bad=usage();bad['total']['totalTokens']=999
        u=normalize(bad,'codex',usage_baseline=counters(0,0,0));self.assertIn('codex_counter_conflict',u['issues'])

    def test_compaction_invalidates_baseline_and_old_context_and_never_asserts_memory_retention(self):
        self.whole(1,usage(5000,1000,100))
        self.job(2);self.event('turn/started',2);self.sample(2,usage(6000,1200,150))
        self.event('item/started',2,item={'type':'contextCompaction','id':'compact-1'})
        self.flush();self.assertIsNone(build_view(self.ledger,'codex','session-a')['context']['tokens'])
        self.event('item/completed',2,item={'type':'contextCompaction','id':'compact-1'})
        self.event('turn/completed',2);self.flush();self.flush()
        row=self.ledger.records('codex')[-1];self.assertFalse(row['usage_complete']);self.assertIsNone(row['usage_baseline'])
        view=build_view(self.ledger,'codex','session-a');self.assertIsNone(view['context']['tokens'])
        compact=view['events'][0];self.assertEqual(compact['before_tokens'],7200);self.assertIsNone(compact['after_tokens'])
        self.assertIsNone(compact['memory_preserved']);self.assertEqual(compact['status'],'completed')

    def test_post_compaction_observation_restores_context_but_not_unverified_turn_sum(self):
        self.whole(1,usage(1000,200,10));self.job(2);self.event('turn/started',2)
        self.event('item/completed',2,item={'type':'contextCompaction','id':'compact-2'})
        self.sample(2,usage(300,60,5,last=counters(300,60,5)));self.event('turn/completed',2);self.flush()
        view=build_view(self.ledger,'codex','session-a');self.assertEqual(view['context']['tokens'],360)
        self.assertEqual(view['events'][0]['after_tokens'],360);self.assertIsNone(view['last_round']['metrics']['input']['value'])

    def test_restart_and_transport_epoch_changes_do_not_reuse_old_baselines(self):
        self.whole(1)
        row=self.whole(2,usage(200,40,10),epoch=2);self.assertIsNone(row['usage_baseline'])
        new=self.make();row=self.whole(3,usage(300,60,15),collector=new,epoch=2)
        self.assertIsNone(row['usage_baseline'])
        row=self.whole(4,usage(400,80,20),collector=new,epoch=2);self.assertEqual(row['normalized']['metrics']['input'],100)

    def test_gap_mid_turn_and_missed_durable_turn_invalidate_scope(self):
        self.whole(1);self.job(2);self.event('turn/started',2)
        self.sample(2,usage(200,40,10),epoch=2);self.event('turn/completed',2,epoch=2);self.flush(epoch=2)
        self.assertIsNone(self.ledger.records('codex')[-1]['usage_baseline'])
        self.whole(3,usage(300,60,15),epoch=2)
        self.job(4,status='completed') # Native turn missed by the observer.
        row=self.whole(5,usage(500,100,25),epoch=2);self.assertIsNone(row['usage_baseline'])

    def test_native_identity_is_idempotent_after_restart_and_replay_cannot_become_baseline(self):
        self.whole(1);new=self.make()
        self.event('turn/started',1,collector=new);self.sample(1,collector=new);self.event('turn/completed',1,collector=new);self.flush(collector=new)
        self.assertEqual(len(self.ledger.records('codex')),1);self.assertEqual(new.health()['captured'],0)
        row=self.whole(2,usage(200,40,10),collector=new);self.assertIsNone(row['usage_baseline'])

    def test_late_changed_usage_never_rewrites_closed_turn_or_seeds_a_new_turn(self):
        self.whole(1);before=copy.deepcopy(self.ledger.records('codex'))
        self.sample(1);self.sample(1,usage(999,90,20));self.flush()
        self.assertEqual(before,self.ledger.records('codex'));self.assertEqual(self.collector.health()['late'],1)
        row=self.whole(2,usage(1100,120,30));self.assertIsNone(row['usage_baseline'])

    def test_sessions_threads_accounts_and_deleted_canonical_messages_are_isolated(self):
        self.whole(1);self.whole(2,thread='thread-b',sid='session-b')
        row=self.whole(3,usage(200,40,10),thread='thread-b',sid='session-b')
        self.assertEqual(row['normalized']['metrics']['input'],100)
        other=self.make('subscription-b');row=self.whole(4,usage(400,80,20),collector=other,thread='thread-b',sid='session-b')
        self.assertIsNone(row['usage_baseline']);self.assertEqual(row['provider_id'],'subscription-b')
        self.job(5);self.event('turn/started',5);self.sample(5);self.event('turn/completed',5)
        with sqlite3.connect(self.relay) as c:c.execute('DELETE FROM messages WHERE id=5')
        self.flush();self.assertFalse(any(r['turn_id']=='turn-5' for r in self.ledger.records('codex')))
        self.assertEqual(self.ledger.records('api'),[])

    def test_raw_usage_secrets_or_oversized_payloads_are_rejected_and_staging_is_bounded(self):
        collector=self.make(max_pending=2)
        self.job(1);self.event('turn/started',1,collector=collector)
        for raw in ({**usage(),'authorization':'synthetic secret'}, {**usage(),'extension':'x'*8193}):
            self.sample(1,raw,collector=collector)
        self.event('turn/completed',1,collector=collector);self.flush(collector=collector)
        self.assertIsNone(self.ledger.records('codex')[0]['raw_usage']);self.assertEqual(collector.health()['dropped'],2)
        for n in (10,11,12):self.event('turn/started',n,collector=collector)
        self.assertEqual(collector.health()['pending'],2);self.assertEqual(collector.health()['evicted'],1)
        self.assertNotIn('synthetic secret',json.dumps(self.ledger.records('codex')))

    def test_failed_interrupted_and_unknown_native_outcomes_preserve_reported_counters(self):
        self.whole(1)
        for n,status in ((2,'failed'),(3,'interrupted'),(4,'new-unknown-status')):
            row=self.whole(n,usage(n*100,n*20,n*5),status=status)
            self.assertEqual(row['status'],{'failed':'error','interrupted':'interrupted'}.get(status,'unknown'))
            self.assertEqual(row['raw_usage']['total']['inputTokens'],n*100)
        self.assertFalse(row['usage_complete']);self.assertIsNone(row['cost']['amount'])

    def test_storage_failure_does_not_change_native_receipt_or_trigger_rpc(self):
        self.job(1);self.event('turn/started',1);self.sample(1);self.event('turn/completed',1)
        with patch.object(self.ledger,'append',side_effect=OSError('synthetic private path')):self.flush()
        self.assertEqual(self.collector.health()['dropped'],1);self.assertEqual(self.ledger.records('codex'),[])
        self.assertNotIn('private path',json.dumps(self.collector.health()))
        self.flush();self.assertEqual(len(self.ledger.records('codex')),1)

    def test_json_csv_exports_keep_raw_totals_and_baseline_separate_from_normalized_delta(self):
        first=usage();self.whole(1,first);raw=usage(300,60,15);self.whole(2,raw)
        rows=self.ledger.records('codex');exported=json.loads(export_records(rows,'json'))['records'][-1]
        self.assertEqual(exported['raw_usage'],raw);self.assertEqual(exported['usage_baseline'],first['total'])
        row=list(csv.DictReader(io.StringIO(export_records(rows,'csv'))))[-1]
        self.assertEqual(row['input'],'200');self.assertEqual(json.loads(row['raw_usage_json'])['total']['inputTokens'],300)
        self.assertEqual(json.loads(row['usage_baseline_json'])['inputTokens'],100)
        self.assertEqual(row['accounting_basis'],'thread_counter_delta');self.assertEqual(row['amount'],'')

    def test_partial_reasoning_and_explicit_reported_zero_remain_distinct_from_unavailable(self):
        first=usage();first['total'].pop('reasoningOutputTokens');self.whole(1,first)
        row=self.whole(2,usage(100,20,0))
        self.assertEqual(row['normalized']['metrics']['input'],0);self.assertIsNone(row['normalized']['metrics']['reasoning'])
        self.assertEqual(build_view(self.ledger,'codex','session-a')['last_round']['metrics']['input']['value'],0)


class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_transport_epoch_changes_only_when_current_owner_is_lost(self):
        from backend.codex_app_server_shared_transport import CodexSharedAppServerRuntime, CodexSharedTransportConfig
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            runtime=CodexSharedAppServerRuntime(CodexSharedTransportConfig(False,root,root,1))
            self.assertEqual(runtime.connection_epoch,0)
            runtime._terminate_owner=AsyncMock()
            owner=object();runtime._process=owner
            await runtime._fail_process('codex_app_server_unavailable',owner=object())
            self.assertEqual(runtime.connection_epoch,0)
            await runtime._fail_process('codex_app_server_unavailable',owner=owner)
            self.assertEqual(runtime.connection_epoch,1);self.assertIsNone(runtime._process)
            runtime._terminate_owner.assert_awaited_once_with(owner)

    async def test_optional_runtime_sink_observes_epoch_and_cannot_fail_generation_callback(self):
        from backend.codex_generation_streaming import StreamingCodexGenerationRuntime
        sink=Mock();fake=SimpleNamespace(usage_collector=sink,foundation=SimpleNamespace(runtime=SimpleNamespace(connection_epoch=7)))
        params={'threadId':'synthetic-thread','turn':{'id':'synthetic-turn'}}
        StreamingCodexGenerationRuntime._on_ledger_usage(fake,'turn/started',params)
        sink.receive.assert_called_once_with('turn/started',params,epoch=7)
        sink.receive.side_effect=OSError('synthetic private error')
        StreamingCodexGenerationRuntime._on_ledger_usage(fake,'turn/started',params)


if __name__=='__main__':unittest.main()
