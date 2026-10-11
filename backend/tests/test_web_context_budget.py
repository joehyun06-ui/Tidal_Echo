import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.web_context_budget import CodexBudgetObserver, read, counter
from backend.web_session_context import delete


class ContextBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db, self.store = str(Path(temp.name) / 'relay.db'), str(Path(temp.name) / 'jobs.db')
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)')
            conn.execute("INSERT INTO messages VALUES (1,'2026-10-12T00:00:00Z','in','user','synthetic',?)", (json.dumps({'api_session':'a'}),))
        with sqlite3.connect(self.store) as conn:
            conn.execute('CREATE TABLE codex_sessions(api_session TEXT,status TEXT,thread_id TEXT,model TEXT)')
            conn.execute("INSERT INTO codex_sessions VALUES ('a','active','thread-a','fixture-model')")
            conn.execute('CREATE TABLE codex_generation_jobs(id INTEGER PRIMARY KEY,api_session TEXT,thread_id TEXT,turn_id TEXT,status TEXT,canonical_message_id INTEGER)')
            conn.execute("INSERT INTO codex_generation_jobs VALUES (1,'a','thread-a','turn-a','in_progress',1)")
        self.observer = CodexBudgetObserver(self.db, self.store)

    def event(self, method='thread/tokenUsage/updated', **changes):
        event = {'threadId':'thread-a', 'turnId':'turn-a', 'tokenUsage':{
            'last':{'inputTokens':1200,'outputTokens':300,'totalTokens':1500},
            'total':{'totalTokens':90000}, 'modelContextWindow':10000}}
        event.update(changes)
        self.observer.receive(method, event)

    def value(self):
        return read(self.db,'a','codex',epoch=self.observer.epoch)

    def test_last_usage_not_cumulative_and_unknown_is_not_zero(self):
        self.assertIsNone(self.value()['used_percent_estimate'])
        self.event()
        value = self.value()
        self.assertEqual(value['used_percent_estimate'],15)
        self.assertEqual(value['remaining_estimate'],8500)
        self.assertFalse(value['stale'])
        self.assertEqual(value['usage']['total_tokens'],1500)
        self.assertNotIn('thread-a', json.dumps(value))
        self.assertNotIn('synthetic', json.dumps(value))
        self.event(tokenUsage={'last':{'inputTokens':0,'totalTokens':0},'modelContextWindow':None})
        self.assertEqual(self.value()['usage']['input_tokens'],0)
        self.assertIsNone(self.value()['used_percent_estimate'])
        for invalid in (True,-1,1.5,'12',2**53):
            self.assertIsNone(counter(invalid))

    def test_no_cross_thread_turn_branch_or_deleted_source(self):
        for changed in ({'threadId':'other'}, {'turnId':'other'}, {'threadId':False}):
            self.event(**changed)
            self.assertIsNone(self.value()['observed_at'])
        self.event()
        self.assertIsNone(read(self.db,'branch','codex')['context_tokens'])
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO messages VALUES (2,'later','in','user','new',?)", (json.dumps({'api_session':'a'}),))
        self.assertTrue(self.value()['stale'])
        with sqlite3.connect(self.db) as conn:
            delete(conn,'a')
            conn.execute('DELETE FROM messages')
        self.event()
        self.assertIsNone(self.value()['context_tokens'])

    def test_compaction_invalidates_old_estimate_and_restart_is_uncertain(self):
        self.event()
        item={'type':'contextCompaction','id':'compact-a','secret':'never retain'}
        self.event('item/started',item=item)
        self.assertIsNone(self.value()['remaining_estimate'])
        self.assertEqual(self.value()['compaction']['status'],'compacting')
        self.assertEqual(read(self.db,'a','codex',epoch='restarted')['compaction']['status'],'unknown')
        self.event('item/completed',item=item)
        self.event('item/started',item=item)
        self.assertEqual(self.value()['compaction']['status'],'completed')
        self.assertIsNone(self.value()['remaining_estimate'])
        self.event(tokenUsage={'last':{'totalTokens':400},'modelContextWindow':10000})
        self.assertEqual(self.value()['remaining_estimate'],9600)
        self.assertNotIn('secret',json.dumps(self.value()))

    def test_unfinished_compaction_does_not_stay_running_after_turn_ends(self):
        self.event('item/started',item={'type':'contextCompaction','id':'compact-a'})
        self.event('turn/completed',turn={'id':'turn-a','status':'failed'})
        self.assertEqual(self.value()['compaction']['status'],'unknown')

    def test_non_compaction_items_never_saved_and_early_ack_events_work(self):
        self.event('item/completed',item={'id':'private','type':'reasoning','text':'private'})
        self.assertIsNone(self.value()['compaction'])
        with sqlite3.connect(self.store) as conn:
            conn.execute("UPDATE codex_generation_jobs SET turn_id=NULL,status='turn_dispatching'")
        self.event()
        self.assertEqual(self.value()['context_tokens'],1500)

    def test_api_usage_is_reported_without_inventing_window_or_sum(self):
        meta={'api_session':'a','source':'api_generation','reply_to':'1',
              'api':{'model':'fixture-api','usage':{'prompt_tokens':12,'completion_tokens':6,'total_tokens':18,'modelContextWindow':999}}}
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO messages VALUES (2,'2026-10-12T00:01:00Z','out','reply','answer',?)",(json.dumps(meta),))
        value=read(self.db,'a','api')
        self.assertEqual(value['usage'],{'input_tokens':12,'output_tokens':6,'total_tokens':18})
        self.assertIsNone(value['context_window'])
        self.assertIsNone(value['remaining_estimate'])
        self.assertFalse(value['stale'])
        with sqlite3.connect(self.db) as conn:
            meta['source']='web_fork'
            conn.execute('UPDATE messages SET meta=? WHERE id=2',(json.dumps(meta),))
        self.assertEqual(read(self.db,'a','api')['usage'],{})


if __name__ == '__main__':
    unittest.main()
