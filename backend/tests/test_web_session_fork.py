import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from backend.web_session_fork import fork_conversation, ForkError
from backend.web_session_provider_authority import WebSessionProviderAuthority
from backend.tests.test_web_session_provider_authority import FakeLegacy

class WebSessionForkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'relay.db')
        self.legacy=FakeLegacy({'sessions':[{'id':'a','title':'原会话','provider':'api'}],'active_session':'a'})
        self.authority=WebSessionProviderAuthority(self.legacy)
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT, kind TEXT, text TEXT, meta TEXT)')
            for direction,kind,text in [('in','user','before'),('out','reply','old answer'),('in','user','typo'),('out','reply','answer'),('in','user','after')]:
                conn.execute('INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)',('2026-10-04',direction,kind,text,json.dumps({'api_session':'a','generation_id':'old','api':{'usage':{'total_tokens':100}}})))
        self.body={'request_id':str(uuid.uuid4()),'message_id':4,'mode':'branch'}
    def rows(self,sid):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT text,meta FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id",(sid,)).fetchall()
    def fork(self,**updates):
        return fork_conversation(self.authority,'a',{**self.body,**updates},relay_db=self.path)
    def test_branch_copies_prefix_only_without_usage_generation_or_active_mutation(self):
        result=self.fork();sid=result['created']['id']
        self.assertEqual([r[0] for r in self.rows(sid)],['before','old answer','typo','answer'])
        self.assertEqual(len(self.rows('a')),5);self.assertEqual(self.legacy.cfg['active_session'],'a')
        for _,meta in self.rows(sid):
            value=json.loads(meta);self.assertNotIn('api',value);self.assertNotIn('generation_id',value);self.assertIn('branch_origin',value)
    def test_edit_and_regenerate_exclude_original_prompt_and_subsequent_answers(self):
        for mode,message_id in [('edit',3),('regenerate',4)]:
            result=self.fork(request_id=str(uuid.uuid4()),mode=mode,message_id=message_id)
            self.assertEqual([r[0] for r in self.rows(result['created']['id'])],['before','old answer'])
        self.assertEqual(len(self.rows('a')),5)
    def test_transport_replay_is_idempotent_and_conflicting_reuse_rejected(self):
        first=self.fork();second=self.fork();self.assertTrue(second['duplicate']);self.assertEqual(first['created']['id'],second['created']['id'])
        self.assertEqual(len(self.rows(first['created']['id'])),4)
        with self.assertRaises(ForkError):self.fork(message_id=2)
    def test_invalid_roles_foreign_message_and_codex_fail_without_publication(self):
        for args in [{'mode':'edit','message_id':2},{'mode':'regenerate','message_id':3},{'message_id':999},{'message_id':True}]:
            with self.assertRaises(ForkError):self.fork(**args)
        self.legacy.cfg['sessions'][0]['provider']='codex'
        with self.assertRaisesRegex(ForkError,'codex_message_fork_unavailable'):self.fork()
        self.assertEqual(len(self.authority.session_rows()),1)
    def test_publication_failure_rolls_back_copy_and_receipt(self):
        original=self.authority.publish_row
        self.authority.publish_row=lambda *a,**k:(_ for _ in ()).throw(OSError('disk'))
        with self.assertRaises(OSError):self.fork()
        self.authority.publish_row=original
        self.assertFalse(self.fork()['duplicate'])
        with sqlite3.connect(self.path) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0],9)
    def test_first_prompt_edit_creates_empty_prefix_and_retains_attachment_references_on_branch(self):
        result=self.fork(mode='edit',message_id=1);self.assertEqual(self.rows(result['created']['id']),[])
        attachment={'url':'/uploads/picture.png','kind':'image'}
        with sqlite3.connect(self.path) as conn:conn.execute("UPDATE messages SET meta=? WHERE id=3",(json.dumps({'api_session':'a','attachments':[attachment]}),))
        result=self.fork(request_id=str(uuid.uuid4()))
        self.assertEqual(json.loads(self.rows(result['created']['id'])[2][1])['attachments'],[attachment])
        with self.assertRaisesRegex(ForkError,'fork_attachment_resend_unavailable'):self.fork(request_id=str(uuid.uuid4()),mode='regenerate')
