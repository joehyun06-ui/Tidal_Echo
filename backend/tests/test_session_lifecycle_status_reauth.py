"""No stale status delivery after authentication changes during the read."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from backend.g3_projection_contract import AccessScope
from backend.session_window_activation_http import install,PATH
from backend.session_window_activation_status import inspect_recorded_activation
from backend.tests.test_session_lifecycle_activation_http import fixture
from backend.tests.test_session_lifecycle_history_http import original_auth,TOKEN
from backend.tests.test_session_lifecycle import CTX


class StatusReauthTests(unittest.IsolatedAsyncioTestCase):
    async def test_changes_during_observation_refuse_result_without_writes(self):
        for change,expected in (('token',401),('owner',403),('scope',403),('invalid',403),('error',503),('none',200)):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                h,journal,body=fixture(Path(directory)/'canonical.sqlite')
                try:
                    check=original_auth();current=[CTX];calls=[]
                    def authenticate(request):
                        calls.append(1);check(request)
                        if current[0]=='error':raise RuntimeError('private authentication detail')
                        return current[0]
                    def observe(*args,**kwargs):
                        result=inspect_recorded_activation(*args,**kwargs)
                        if change=='token':check.__globals__['API_LOOP_INTERNAL_TOKEN']='rotated'
                        if change=='owner':current[0]=AccessScope('other',CTX.allowed_scopes)
                        if change=='scope':current[0]=AccessScope(CTX.principal_id,frozenset())
                        if change=='invalid':current[0]=None
                        if change=='error':current[0]='error'
                        return result
                    app=FastAPI();install(app,journal,authenticate=authenticate,enabled=True)
                    before=list(h.conn.iterdump());config=h.config_path.read_bytes()
                    with patch('backend.session_window_activation_http.inspect_recorded_activation',side_effect=observe):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.invalid') as client:
                            response=await client.post(PATH,json=body,headers={'x-api-loop-internal-token':TOKEN})
                    self.assertEqual(response.status_code,expected,response.text)
                    self.assertEqual(len(calls),2)
                    self.assertEqual(response.headers['cache-control'],'private, no-store')
                    if expected!=200:self.assertNotIn('state',response.json())
                    self.assertNotIn('private authentication detail',response.text)
                    self.assertEqual(list(h.conn.iterdump()),before);self.assertEqual(h.config_path.read_bytes(),config)
                finally:h.conn.close()
