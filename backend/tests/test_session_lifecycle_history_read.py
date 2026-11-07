"""Coordinated count projection and explicit strict HTTP read admission."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from backend.session_lifecycle_history_http import install_api_loop,PREFIX
from backend.session_lifecycle_http_interlock import install_api_loop_interlock
from backend.session_lifecycle_history_read import read_history
from backend.session_lifecycle_coordination import digest
from backend.tests import test_session_lifecycle_history_http as web
from backend.tests.test_session_lifecycle_history_packet import binding


class HistoryReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=web.fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.a=binding(self.f.h.path,self.directory);self.app=FastAPI();self.effects=[]
        @self.app.get('/legacy-read')
        async def legacy():self.effects.append('read');return {'secret':'must not read'}
        loop=SimpleNamespace(app=self.app,check_internal_auth=web.original_auth())
        install_api_loop(loop,self.a,principal_id='synthetic_owner',enabled=True)
        install_api_loop_interlock(loop,enabled=True,strict_reads=True)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic.invalid',
            headers={'x-api-loop-internal-token':web.TOKEN});self.addAsyncCleanup(self.client.aclose)
    async def ref(self):
        r=await self.client.post(PREFIX+'/prepare',json={'request_id':'read-test','history_n':73})
        self.assertEqual(r.status_code,200,r.text)
        return {k:r.json()[k] for k in ('request_id','packet_sha')}
    def snapshot(self):return (self.f.h.config_path.read_bytes(),self.f.generation(),self.f.h.conn.execute('SELECT * FROM session_coordination_ops').fetchall())
    async def test_read_projects_only_count_and_current_generation_without_effects(self):
        before=self.snapshot();r=await self.client.get(PREFIX)
        self.assertEqual(r.json(),{'ok':True,'history_n':24,'generation':0,'observed':True})
        self.assertEqual(before,self.snapshot());self.assertEqual(r.headers['cache-control'],'private, no-store')
        self.assertNotIn(web.fixtures.ROUTE['key'],r.text);self.assertEqual(list(self.directory.iterdir()),[])
    async def test_default_helper_disabled_precedes_dependencies(self):
        with self.assertRaises(web.fixtures.LifecycleError) as e:read_history(None,access=None)
        self.assertEqual(e.exception.category,'lifecycle_disabled')
        self.assertFalse(install_api_loop_interlock(None,strict_reads=True))
    async def test_after_original_execution_read_observes_new_count_and_generation(self):
        ref=await self.ref();self.assertEqual((await self.client.post(PREFIX+'/execute',json={**ref,'recover':False})).status_code,200)
        r=await self.client.get(PREFIX);self.assertEqual(r.json()['history_n'],73);self.assertEqual(r.json()['generation'],1)
    async def test_pending_intent_refuses_config_snapshot_instead_of_showing_old_count(self):
        ref=await self.ref()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            await self.client.post(PREFIX+'/execute',json={**ref,'recover':False})
        before=self.snapshot();r=await self.client.get(PREFIX)
        self.assertEqual(r.status_code,409);self.assertNotIn('history_n',r.json());self.assertEqual(before,self.snapshot())
    async def test_external_config_change_refuses_without_leaking_private_data(self):
        cfg=self.f.h.get('legacy');cfg['history_n']=13;self.f.h.put('legacy',cfg)
        r=await self.client.get(PREFIX);self.assertEqual(r.status_code,503)
        self.assertNotIn('history_n',r.json());self.assertNotIn(str(self.f.root),r.text)
    async def test_invalid_legacy_count_is_unavailable_not_zero_or_coerced(self):
        cfg=self.f.h.get('legacy');cfg['history_n']='24';self.f.h.put('legacy',cfg)
        # Synthetic preexisting invalid value with a matching trusted journal hash.
        self.f.h.conn.execute('UPDATE session_coordination_meta SET config_sha=? WHERE id=1',(digest(self.f.h.config_path.read_bytes()),))
        r=await self.client.get(PREFIX);self.assertEqual(r.status_code,503);self.assertNotIn('history_n',r.json())
    async def test_current_acl_and_final_guard_prevent_read_publication(self):
        calls=[]
        self.a.commands._authorize=lambda *_:calls.append(1) or len(calls)<3
        r=await self.client.get(PREFIX);self.assertEqual(r.status_code,403);self.assertEqual(len(calls),3)
        self.assertNotIn('history_n',r.json());self.assertEqual(self.f.generation(),0)
    async def test_auth_and_request_shape_precede_projection(self):
        with patch.object(self.a.commands.coordinator,'observe_config',side_effect=AssertionError('projection read')):
            r=await self.client.get(PREFIX,headers={'x-api-loop-internal-token':'wrong'});self.assertEqual(r.status_code,401)
            self.assertEqual((await self.client.get(PREFIX+'?path=other')).status_code,400)
            self.assertEqual((await self.client.request('GET',PREFIX,content='{}')).status_code,400)
    async def test_strict_reads_refuse_legacy_and_encoded_alias_without_entering_handler(self):
        for method,path in (('GET','/legacy-read'),('HEAD','/legacy-read'),('OPTIONS','/legacy-read'),('GET','/loop/lifecycle/%68istory')):
            r=await self.client.request(method,path);self.assertEqual(r.status_code,423)
        self.assertEqual(self.effects,[])


if __name__=='__main__':unittest.main()
