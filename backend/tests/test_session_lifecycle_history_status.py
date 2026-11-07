"""Original-reference status through actual ASGI; never initiates recovery."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from backend import session_lifecycle_coordination as coordination
from backend.session_lifecycle_history_http import install_api_loop,PREFIX
from backend.session_lifecycle_history_status import inspect_history
from backend.session_lifecycle_request_packets import PacketReference
from backend.tests import test_session_lifecycle_history_http as web
from backend.tests.test_session_lifecycle_history_packet import binding


@unittest.skipUnless(os.name=='posix','private POSIX status')
class HistoryStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=web.fixtures.ModelCommandTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.directory=self.f.root/'packets';self.directory.mkdir(mode=0o700)
        self.a=binding(self.f.h.path,self.directory)
        app=FastAPI();install_api_loop(SimpleNamespace(app=app,check_internal_auth=web.original_auth()),
            self.a,principal_id='synthetic_owner',enabled=True)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid',
            headers={'x-api-loop-internal-token':web.TOKEN});self.addAsyncCleanup(self.client.aclose)
    async def ref(self):
        r=await self.client.post(PREFIX+'/prepare',json={'request_id':'status-request','history_n':73})
        self.assertEqual(r.status_code,200,r.text)
        return {k:r.json()[k] for k in ('request_id','packet_sha')}
    async def execute(self,ref,recover=False):return await self.client.post(PREFIX+'/execute',json={**ref,'recover':recover})
    async def status(self,ref):return await self.client.post(PREFIX+'/status',json=ref)
    def snapshot(self):
        return (self.f.h.config_path.read_bytes(),self.f.generation(),
            self.f.h.conn.execute('SELECT * FROM session_coordination_ops ORDER BY request_id').fetchall(),
            sorted((p.name,p.read_bytes()) for p in (self.f.root/'gate').glob('*.stage')),
            sorted((p.name,p.read_bytes()) for p in self.directory.glob('*.json')))
    async def test_default_status_disabled_before_dependency_access(self):
        with self.assertRaises(web.fixtures.LifecycleError) as e:
            inspect_history(None,None,access=None)
        self.assertEqual(e.exception.category,'lifecycle_disabled')
    async def test_prepared_status_is_no_effect_and_not_a_safe_retry_promise(self):
        ref=await self.ref();before=self.snapshot()
        with (patch.object(self.a.commands.coordinator,'recover',side_effect=AssertionError('recover')),
              patch.object(self.a.commands.coordinator,'mutate',side_effect=AssertionError('mutate'))):
            r=await self.status(ref)
        self.assertEqual(r.json(),{'ok':True,'state':'prepared','generation':None,'historical':False})
        self.assertEqual(before,self.snapshot());self.assertEqual(r.headers['cache-control'],'private, no-store')
    async def test_pending_observation_leaves_original_intent_and_stage_untouched(self):
        ref=await self.ref()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('private path')):
            self.assertEqual((await self.execute(ref)).status_code,503)
        before=self.snapshot();r=await self.status(ref)
        self.assertEqual(r.json()['state'],'pending');self.assertIsNone(r.json()['generation'])
        self.assertEqual(before,self.snapshot())
        self.assertEqual((await self.execute(ref,True)).status_code,200)
        self.assertEqual((await self.status(ref)).json()['state'],'committed')
    async def test_json_already_published_does_not_forge_committed_receipt(self):
        ref=await self.ref();original=coordination.atomic_write_text
        def publish_then_fail(*args):original(*args);raise OSError('uncertain')
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=publish_then_fail):
            self.assertEqual((await self.execute(ref)).status_code,503)
        self.assertEqual(self.f.h.get('legacy')['history_n'],73);self.assertEqual(self.f.generation(),0)
        self.assertEqual((await self.status(ref)).json()['state'],'pending');self.assertEqual(self.f.generation(),0)
    async def test_historical_commit_status_keeps_later_config_and_does_not_clean_stage(self):
        ref=await self.ref();await self.execute(ref)
        self.a.commands.update_config('later',{'history_n':5},access=web.ConfigCommandAccess('synthetic_owner'))
        stage=self.f.h.conn.execute("SELECT stage FROM session_coordination_ops WHERE request_id='status-request'").fetchone()[0]
        orphan=self.f.root/'gate'/stage;orphan.write_text('synthetic orphan');orphan.chmod(0o600)
        before=self.snapshot();r=await self.status(ref)
        self.assertEqual(r.json(),{'ok':True,'state':'committed','generation':1,'historical':True})
        self.assertEqual(before,self.snapshot());self.assertTrue(orphan.exists())
    async def test_wrong_reference_missing_packet_and_extra_fields_refuse(self):
        ref=await self.ref()
        self.assertEqual((await self.status({**ref,'packet_sha':'0'*64})).status_code,409)
        self.assertEqual((await self.status({**ref,'recover':True})).status_code,400)
        self.assertEqual((await self.status({**ref,'request_id':'missing'})).status_code,409)
    async def test_current_command_and_packet_acl_both_apply_to_status(self):
        ref=await self.ref();self.a.commands._authorize=lambda *_:False
        self.assertEqual((await self.status(ref)).status_code,403)
        self.a.commands._authorize=lambda *_:True;self.a.packets.authorize=lambda *_:False
        self.assertEqual((await self.status(ref)).status_code,403)
    async def test_auth_precedes_status_body_and_private_store(self):
        self.a.packets.directory=Path('/unavailable')
        r=await self.client.post(PREFIX+'/status',content='not json',headers={'x-api-loop-internal-token':'wrong'})
        self.assertEqual(r.status_code,401);self.assertEqual(r.headers['referrer-policy'],'no-referrer')
    async def test_changed_server_policy_and_external_json_cannot_report_success(self):
        ref=await self.ref();await self.execute(ref);self.a.commands._routes=lambda:[]
        self.assertEqual((await self.status(ref)).status_code,409)
        self.a.commands._routes=web.commands(self.a.commands.coordinator)._routes
        cfg=self.f.h.get('legacy');cfg['external']=True;self.f.h.put('legacy',cfg)
        r=await self.status(ref);self.assertEqual(r.status_code,503)
        self.assertNotIn(str(self.f.root),r.text);self.assertNotIn(web.fixtures.ROUTE['key'],r.text)
    async def test_missing_pending_stage_never_publishes_or_claims_commit(self):
        ref=await self.ref()
        with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('interrupted')):
            await self.execute(ref)
        for p in (self.f.root/'gate').glob('*.stage'):p.unlink()
        before=self.snapshot();r=await self.status(ref)
        self.assertEqual(r.status_code,503);self.assertEqual(before,self.snapshot())


if __name__=='__main__':unittest.main()
