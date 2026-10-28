"""Synthetic histories only; no provider, memory or production data access."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.usage_ledger import Ledger, build_view
from backend.usage_ledger_sources import MessageCounts


class MessageSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'relay.sqlite'
        self.ledger = Ledger(Path(tmp.name)/'ledger.sqlite')
        self.rows = {sid: {'id': sid, 'provider': mode} for sid, mode in
                     [('root','api'), ('alt','api'), ('nested','api'), ('branch','api'), ('native','codex')]}
        self.source = MessageCounts(self.path, session_row=self.rows.get)
        with sqlite3.connect(self.path) as conn:
            conn.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY, direction TEXT, kind TEXT, text TEXT, meta TEXT);
                CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY,root_id TEXT,parent_id TEXT,mode TEXT);
                CREATE TABLE web_version_selections(root_id TEXT PRIMARY KEY,version_id TEXT);''')

    def message(self, sid, kind='user', direction='in', **meta):
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO messages(direction,kind,text,meta) VALUES (?,?,?,?)',
                         (direction, kind, 'synthetic private text never selected', json.dumps({'api_session':sid, **meta})))

    def sql(self, query, params=()):
        with sqlite3.connect(self.path) as conn: conn.execute(query, params)

    def test_empty_history_is_known_zero_but_archive_and_folding_unknown(self):
        result = self.source('api','root')
        self.assertEqual(result['counts'], {'total':0,'visible':0,'turns':0,'folded':None,'archived':None})
        self.assertEqual(result['provenance']['status'],'available')

    def test_counts_are_messages_not_requests_or_dom_nodes_and_ignore_non_chat_roles(self):
        for kind, direction in [('user','in'),('voice','in'),('reply','out'),('tool','out'),('thinking','out'),('user','out'),('reply','in')]:
            self.message('root',kind,direction)
        self.message('branch'); self.message('native')
        self.assertEqual(self.source('api','root')['counts']['total'],3)
        self.assertEqual(self.source('api','root')['counts']['turns'],2)
        self.assertEqual(self.source('codex','native')['counts']['total'],1)
        self.assertIsNone(self.source('codex','root')['counts']['total'])

    def test_history_has_no_page_or_fork_cap(self):
        with sqlite3.connect(self.path) as conn:
            conn.executemany("INSERT INTO messages(direction,kind,meta) VALUES ('in','user',?)",
                             [(json.dumps({'api_session':'root'}),)]*6001)
        self.assertEqual(self.source('api','root')['counts']['total'],6001)

    def test_selected_alternative_includes_prefix_once_and_not_other_versions(self):
        for sid in ('root','alt','nested','branch'): self.message(sid)
        self.message('nested','reply','out',version_copy=True,branch_origin={'session_id':'alt','message_id':2})
        self.sql("INSERT INTO web_message_versions VALUES ('alt','root','root','edit')")
        self.sql("INSERT INTO web_message_versions VALUES ('nested','root','alt','regenerate')")
        self.sql("INSERT INTO web_version_selections VALUES ('root','nested')")
        for sid in ('root','alt','nested'):
            result=self.source('api',sid)
            self.assertEqual(result['counts']['total'],2)
            self.assertEqual(result['provenance']['copied_messages'],1)
            self.assertEqual(result['provenance']['selected_session_id'],'nested')
        self.assertEqual(self.source('api','branch')['counts']['total'],1)
        self.sql("UPDATE web_version_selections SET version_id='root'")
        self.assertEqual(self.source('api','root')['counts']['total'],1)

    def test_invalid_selection_missing_deleted_or_cross_provider_never_falls_back(self):
        self.message('root')
        self.sql("INSERT INTO web_version_selections VALUES ('root','alt')")
        self.assertIsNone(self.source('api','root')['counts']['total'])
        self.sql("INSERT INTO web_message_versions VALUES ('alt','root','root','edit')")
        self.rows['alt']['provider']='codex'
        self.assertIsNone(self.source('api','root')['counts']['total'])
        self.rows.pop('alt')
        self.assertIsNone(self.source('api','root')['counts']['total'])

    def test_parent_cycles_and_invalid_modes_fail_closed(self):
        self.sql("INSERT INTO web_version_selections VALUES ('root','alt')")
        self.sql("INSERT INTO web_message_versions VALUES ('alt','root','alt','edit')")
        self.assertIsNone(self.source('api','root')['counts']['total'])
        self.sql("UPDATE web_message_versions SET parent_id='root',mode='unexpected'")
        self.assertIsNone(self.source('api','root')['counts']['total'])

    def test_missing_store_is_not_created_and_partial_version_schema_unknown(self):
        missing=self.path.with_name('absent.sqlite')
        self.assertIsNone(MessageCounts(missing,session_row=self.rows.get)('api','root')['counts']['total'])
        self.assertFalse(missing.exists())
        self.sql('DROP TABLE web_version_selections')
        self.assertEqual(self.source('api','root')['provenance']['reason'],'version_store_incomplete')

    def test_no_version_schema_is_supported_without_migration(self):
        self.sql('DROP TABLE web_version_selections'); self.sql('DROP TABLE web_message_versions')
        self.message('root')
        self.assertEqual(self.source('api','root')['counts']['total'],1)

    def test_authority_change_during_snapshot_invalidates_counts(self):
        calls=0
        def resolver(sid):
            nonlocal calls
            calls+=1
            return self.rows.get(sid) if calls<4 else None
        result=MessageCounts(self.path,session_row=resolver)('api','root')
        self.assertEqual(result['provenance']['reason'],'session_changed')

    def test_source_is_read_only_does_not_select_text_and_never_touches_network(self):
        self.message('root')
        connect=sqlite3.connect
        reads=[]
        def guarded(*args,**kwargs):
            conn=connect(*args,**kwargs)
            def authorize(action,table,column,*rest):
                if action==sqlite3.SQLITE_READ:
                    reads.append((table,column))
                    if table=='messages' and column not in ('id','direction','kind','meta',''):
                        return sqlite3.SQLITE_DENY
                if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        with patch('backend.usage_ledger_sources.sqlite3.connect',side_effect=guarded), patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            self.assertEqual(self.source('api','root')['counts']['total'],1)
        self.assertNotIn(('messages','text'),reads)

    def test_mode_all_scope_and_model_filters_do_not_mix_or_hide_history(self):
        self.message('root')
        self.ledger.event({'event_id':'old-snapshot','session_id':'root','mode':'api','kind':'message_counts',
                           'status':'observed','at':'2026-10-23T02:00:00Z','snapshot':{'total':999,'turns':999}})
        result=build_view(self.ledger,'api','root',model='missing-model',message_source=self.source)
        self.assertEqual(result['message_counts']['total'],1)
        self.assertIsNone(build_view(self.ledger,'api','root',all_sessions=True,message_source=self.source)['message_counts']['total'])
        self.assertIsNone(build_view(self.ledger,'codex','root',message_source=self.source)['message_counts']['total'])


class SourceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_source_is_read_after_auth_and_threaded_with_scope(self):
        import httpx
        from fastapi import FastAPI,HTTPException
        from backend.usage_ledger_routes import install
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'ledger.sqlite'); app=FastAPI(); calls=[]
            def source(mode,sid,**kw):
                calls.append((mode,sid,kw))
                return {'counts':{'total':4,'visible':4,'turns':2},'provenance':{'source':'offline_mock','status':'available'}}
            def auth(request):
                if request.headers.get('authorization')!='Bearer synthetic': raise HTTPException(401)
            install(app,ledger=ledger,check_auth=auth,session_provider=lambda sid:'api',message_source=source)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.test') as client:
                self.assertEqual((await client.get('/app/sessions/root/usage-ledger')).status_code,401)
                self.assertEqual(calls,[])
                response=await client.get('/app/sessions/root/usage-ledger',headers={'Authorization':'Bearer synthetic'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['message_counts']['total'],4)
                self.assertIsNone(response.json()['message_counts']['archived'])
                self.assertEqual(calls,[('api','root',{'all_sessions':False})])


if __name__=='__main__': unittest.main()
