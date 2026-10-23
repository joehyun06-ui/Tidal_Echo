"""Continuation truth comes from local durable receipts, never SDK calls."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.usage_ledger import Ledger, LedgerError
from backend.usage_ledger_continuation import Continuations


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.relay=self.root/'relay.sqlite'; self.native=self.root/'native.sqlite'
        self.ledger=Ledger(self.root/'ledger.sqlite')
        self.rows={sid:{'id':sid,'provider':mode} for sid,mode in [('source','api'),('target','api'),('native-source','codex'),('native-target','codex')]}
        self.request='11111111-1111-4111-8111-111111111111'
        with sqlite3.connect(self.relay) as conn:
            conn.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,direction TEXT,kind TEXT,text TEXT,meta TEXT);
                CREATE TABLE api_web_generations(message_id INTEGER PRIMARY KEY,session_id TEXT,status TEXT,reply_id INTEGER,upstream_unknown INTEGER);
                CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY,root_id TEXT,parent_id TEXT,mode TEXT);''')
        with sqlite3.connect(self.native) as conn:
            conn.execute('CREATE TABLE codex_generation_jobs(api_session TEXT,canonical_message_id INTEGER,status TEXT,assistant_message_id INTEGER,generation_id TEXT,callback_identity TEXT,client_message_id TEXT)')
        self.adapter=Continuations(self.ledger,relay_db=self.relay,native_db=self.native,session_row=self.rows.get,digest_key=b'synthetic-only-key-for-offline-test')

    def register(self, mode='api', text='synthetic editable draft'):
        return self.adapter.register(source_id='source' if mode=='api' else 'native-source',target_id='target' if mode=='api' else 'native-target',mode=mode,request_id=self.request,expected_text=text)

    def incoming(self, text='synthetic editable draft', mode='api', mid=1, **meta):
        with sqlite3.connect(self.relay) as conn:
            conn.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(mid,'in','user',text,json.dumps({'api_session':'target' if mode=='api' else 'native-target','channel':'web','source':'relay',**meta})))

    def completed(self, mode='api', status='completed', reply_id=2, **meta):
        sid='target' if mode=='api' else 'native-target'
        data={'channel':'web','source':mode+'_generation','provider':mode,'api_session':sid,'reply_to':'1','finish_reason':'completed','generation_id':'api-gen-1' if mode=='api' else 'native-generation'}
        if mode=='codex':data.update(codex_callback_identity='callback-1',client_message_id='client-1')
        data.update(meta)
        with sqlite3.connect(self.relay) as conn:
            conn.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(reply_id,'out','reply','synthetic reply',json.dumps(data)))
            if mode=='api':conn.execute('INSERT INTO api_web_generations VALUES (?,?,?,?,?)',(1,sid,status,reply_id,0))
        if mode=='codex':
            with sqlite3.connect(self.native) as conn:
                conn.execute('INSERT INTO codex_generation_jobs VALUES (?,?,?,?,?,?,?)',(sid,1,status,reply_id,'native-generation','callback-1','client-1'))

    def events(self, mode='api'):
        self.adapter.reconcile('source' if mode=='api' else 'native-source',mode)
        return self.ledger.events(mode)

    def stages(self, mode='api'): return [e['snapshot']['stage'] for e in self.events(mode)]

    def test_new_target_is_observed_not_successful_or_draft_delivered(self):
        self.assertTrue(self.register()); events=self.events()
        self.assertEqual([e['snapshot']['stage'] for e in events],['target_created'])
        self.assertEqual(events[0]['status'],'observed'); self.assertIsNone(events[0]['snapshot']['success'])
        self.assertIsNone(events[0].get('memory_preserved'))

    def test_received_then_actual_completed_first_reply_is_success_and_replay_safe(self):
        self.register(); self.incoming()
        self.assertEqual(self.stages(),['target_created','message_received'])
        self.completed()
        events=self.events(); self.assertEqual(len(events),3)
        self.assertTrue(events[-1]['snapshot']['success']); self.assertEqual(events[-1]['snapshot']['reply_message_id'],2)
        self.assertFalse(self.register()); self.assertEqual(len(self.events()),3)
        self.assertTrue(all(e.get('memory_preserved') is None for e in events))

    def test_mismatched_first_message_does_not_match_a_later_message(self):
        self.register(); self.incoming('edited differently'); self.incoming(mid=3); self.completed()
        self.assertEqual(self.stages(),['target_created','content_unverified'])
        self.assertIsNone(self.events()[-1]['snapshot']['success'])

    def test_native_completion_requires_callback_commit_and_exact_all_identities(self):
        self.register('codex'); self.incoming(mode='codex'); self.completed('codex',status='callback_pending')
        self.assertEqual(self.stages('codex'),['target_created','message_received'])
        with sqlite3.connect(self.native) as conn:conn.execute("UPDATE codex_generation_jobs SET status='completed'")
        self.assertEqual(self.stages('codex')[-1],'reply_completed')
        self.assertEqual(self.ledger.events('api'),[])

    def test_wrong_native_callback_or_partial_reply_never_becomes_success(self):
        self.register('codex'); self.incoming(mode='codex'); self.completed('codex',codex_callback_identity='wrong')
        self.assertEqual(self.stages('codex'),['target_created','message_received'])
        with sqlite3.connect(self.relay) as conn:
            row=conn.execute('SELECT meta FROM messages WHERE id=2').fetchone();meta=json.loads(row[0]);meta.update(codex_callback_identity='callback-1',finish_reason='interrupted')
            conn.execute('UPDATE messages SET meta=? WHERE id=2',(json.dumps(meta),))
        self.assertEqual(self.stages('codex'),['target_created','message_received'])

    def test_failed_job_and_partial_output_are_failure_not_success(self):
        self.register(); self.incoming(); self.completed(status='failed')
        events=self.events();self.assertEqual(events[-1]['snapshot']['stage'],'reply_failed');self.assertIs(events[-1]['snapshot']['success'],False)

    def test_unknown_upstream_and_wrong_reply_links_do_not_become_success(self):
        self.register(); self.incoming(); self.completed(reply_to='999')
        self.assertEqual(self.stages(),['target_created','message_received'])
        with sqlite3.connect(self.relay) as conn:
            meta=json.loads(conn.execute('SELECT meta FROM messages WHERE id=2').fetchone()[0]);meta['reply_to']='1'
            conn.execute('UPDATE messages SET meta=? WHERE id=2',(json.dumps(meta),))
            conn.execute('UPDATE api_web_generations SET upstream_unknown=1')
        self.assertEqual(self.stages(),['target_created','message_received'])

    def test_corrupt_empty_copied_or_wrong_session_reply_is_unconfirmed(self):
        self.register();self.incoming();self.completed()
        with sqlite3.connect(self.relay) as conn:
            original=conn.execute('SELECT meta FROM messages WHERE id=2').fetchone()[0]
        for meta in ('null','[]','not json',json.dumps({**json.loads(original),'api_session':'source'}),json.dumps({**json.loads(original),'version_copy':True})):
            with self.subTest(meta=meta):
                with sqlite3.connect(self.relay) as conn:conn.execute('UPDATE messages SET meta=? WHERE id=2',(meta,))
                self.assertEqual(self.stages(),['target_created','message_received'])
        with sqlite3.connect(self.relay) as conn:conn.execute("UPDATE messages SET meta=?,text='' WHERE id=2",(original,))
        self.assertEqual(self.stages(),['target_created','message_received'])

    def test_missing_completion_store_still_records_message_receipt(self):
        self.register();self.incoming()
        with sqlite3.connect(self.relay) as conn:conn.execute('DROP TABLE api_web_generations')
        self.assertEqual(self.stages(),['target_created','message_received'])

    def test_copied_history_cannot_prove_the_draft_was_sent(self):
        self.register();self.incoming(version_copy=True);self.completed()
        self.assertEqual(self.stages(),['target_created','content_unverified'])

    def test_deleted_target_or_mode_mismatch_is_not_processed(self):
        self.register();self.incoming();self.completed();self.rows.pop('target')
        self.assertEqual(self.events(),[])
        self.rows['target']={'id':'target','provider':'codex'}
        self.assertEqual(self.events(),[])

    def test_registration_is_trusted_idempotent_but_rejects_conflicts_or_existing_history(self):
        self.register()
        with self.assertRaisesRegex(LedgerError,'conflict'):self.register(text='different')
        with self.assertRaisesRegex(LedgerError,'session_unavailable'):
            self.adapter.register(source_id='source',target_id='native-target',mode='api',request_id=self.request,expected_text='draft')
        self.incoming(mode='codex')
        self.request='22222222-2222-4222-8222-222222222222'
        with self.assertRaisesRegex(LedgerError,'target_not_empty'):self.register('codex')

    def test_no_plaintext_or_key_in_receipts_events_and_network_is_unused(self):
        with patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            self.register();self.incoming();self.completed();self.events()
        encoded=Path(self.ledger.path).read_bytes()
        self.assertNotIn(b'synthetic editable draft',encoded);self.assertNotIn(b'synthetic reply',encoded);self.assertNotIn(self.adapter.key,encoded)

    def test_restart_same_key_recovers_without_draft_and_rotated_key_cannot_guess(self):
        self.register();self.incoming()
        self.adapter=Continuations(self.ledger,relay_db=self.relay,session_row=self.rows.get,digest_key=self.adapter.key)
        self.assertEqual(self.stages(),['target_created','message_received'])
        self.ledger.purge_session('source')
        with sqlite3.connect(self.relay) as conn:conn.execute('DELETE FROM messages')
        self.register();self.incoming()
        self.adapter=Continuations(self.ledger,relay_db=self.relay,session_row=self.rows.get,digest_key=b'rotated-synthetic-key-not-the-same-key')
        self.assertEqual(self.stages(),['target_created'])

    def test_purge_source_or_target_removes_receipt_and_cannot_resurrect_events(self):
        self.register();self.incoming();self.completed();self.events()
        self.ledger.purge_session('target')
        self.assertEqual(self.events(),[])
        with closing(self.ledger.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_ledger_continuations').fetchone()[0],0)

    def test_target_version_is_rejected_before_registration(self):
        with sqlite3.connect(self.relay) as conn:conn.execute("INSERT INTO web_message_versions VALUES ('target','source','source','edit')")
        with self.assertRaisesRegex(LedgerError,'target_is_version'):self.register()

    def test_unrecognized_key_short_draft_and_invalid_request_are_rejected(self):
        with self.assertRaisesRegex(LedgerError,'key_invalid'):
            Continuations(self.ledger,relay_db=self.relay,session_row=self.rows.get,digest_key=b'short')
        for text in ('', ' '*3, 'x'*12001):
            with self.assertRaisesRegex(LedgerError,'request_invalid'):self.register(text=text)

    def test_event_contract_cannot_call_target_creation_completed(self):
        with self.assertRaisesRegex(LedgerError,'event_invalid'):
            self.ledger.event({'event_id':'bad','session_id':'source','target_session_id':'target','mode':'api','kind':'continuation','status':'completed','at':'2026-10-23T01:00:00Z',
                               'source':'canonical_continuation_receipt','snapshot':{'stage':'target_created','success':True,'input_message_id':None,'reply_message_id':None}})


if __name__=='__main__':unittest.main()
