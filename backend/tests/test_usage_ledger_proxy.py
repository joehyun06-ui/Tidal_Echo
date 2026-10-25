"""Actual relay/loop ASGI chain and HTTPX streaming faults; outbound forbidden."""
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from backend.usage_ledger import LedgerError
from backend.usage_ledger_proxy import install
from backend.tests.test_usage_ledger_composition import CompositionFixture, ROUTE, RAW

TOKEN = 'synthetic-internal-loop-token-only-12345'


class TraceStream(httpx.AsyncByteStream):
    def __init__(self, *, fail=False): self.reads=0; self.closes=0; self.fail=fail
    async def __aiter__(self):
        self.reads += 1; yield b'x'*65536
        self.reads += 1
        if self.fail: raise httpx.ReadError('synthetic-private-stream-detail')
        yield b'y'*65536
    async def aclose(self): self.closes += 1


class ProxyTests(CompositionFixture):
    def make(self, handler=None):
        self.calls=[]; self.clients=[]; self.client_options=[]
        loop=FastAPI(); self.reader=self.bundle()
        def internal(request):
            if request.headers.get('x-api-loop-internal-token') != TOKEN: raise HTTPException(401)
        self.reader.install_reader(loop, check_auth=internal, prefix='/loop')
        transport=httpx.ASGITransport(app=loop)
        async def dispatch(request):
            self.calls.append(request)
            self.assertEqual(request.headers.get('x-api-loop-internal-token'), TOKEN)
            for key in ('authorization', 'cookie', 'origin'): self.assertNotIn(key, request.headers)
            if handler is not None:
                value=handler(request)
                return await value if hasattr(value, '__await__') else value
            return await transport.handle_async_request(request)
        def factory(**options):
            self.client_options.append(options)
            client=httpx.AsyncClient(transport=httpx.MockTransport(dispatch), **options)
            self.clients.append(client); return client
        def auth(request):
            if request.headers.get('authorization') != 'Bearer synthetic-browser-only': raise HTTPException(401)
        app=FastAPI()
        install(app, enabled=True, check_auth=auth, base_url='http://127.0.0.1:3020', internal_token=TOKEN, client_factory=factory)
        self.app=app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://offline',
                                 headers={'Authorization':'Bearer synthetic-browser-only', 'Cookie':'synthetic-private-cookie', 'Origin':'http://offline'})

    def test_disabled_installer_has_no_config_app_or_client_side_effects(self):
        self.assertFalse(install(object(), base_url=object(), internal_token=object(), client_factory=object()))
        with self.assertRaisesRegex(LedgerError, 'config_invalid'): install(object(), enabled='false')

    def test_non_loopback_origins_and_header_injection_are_rejected(self):
        for base in ('https://api.openai.com', 'http://localhost:3020', 'http://example.invalid:3020',
                     'http://127.0.0.1:3020/extra', 'http://user:pass@127.0.0.1:3020',
                     'http://127.0.0.1:3020?url=private', 'http://127.0.0.1:3020#fragment'):
            with self.assertRaisesRegex(LedgerError, 'config_invalid'):
                install(FastAPI(), enabled=True, check_auth=lambda r: None, base_url=base, internal_token=TOKEN)
        for token in ('short', TOKEN+'\n', '密'*40):
            with self.assertRaises(LedgerError):
                install(FastAPI(), enabled=True, check_auth=lambda r: None, base_url='http://127.0.0.1:3020', internal_token=token)

    async def test_real_authenticated_json_pages_export_and_cas_settings_use_loop_authority(self):
        producer=self.bundle(capture=True)
        attempt=producer.api.begin(ROUTE, 'chat', 'a', 'synthetic-turn'); attempt.usage(RAW); await attempt.finish(None)
        async with self.make() as client:
            response=await client.get('/app/sessions/a/usage-ledger?mode=api')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['summary']['metrics']['input']['value'], 100)
            self.assertIn('no-store', response.headers['cache-control'])
            page=(await client.get('/app/sessions/a/usage-ledger/records?mode=api&limit=1')).json()
            self.assertEqual(len(page['records']), 1); self.assertEqual(page['records'][0]['raw_usage'], RAW)
            exported=await client.get('/app/sessions/a/usage-ledger/export?mode=api&format=json')
            self.assertEqual(exported.json()['records'][0]['raw_usage'], RAW)
            csv=await client.get('/app/sessions/a/usage-ledger/export?mode=api&format=csv')
            self.assertIn('text/csv', csv.headers['content-type']); self.assertIn('request_id', csv.text)
            body={'revision':0, 'policy':{'threshold_tokens':None,'warning_margin_tokens':None,'timezone':'UTC','budgets':[]}}
            saved=await client.put('/app/usage-ledger/settings/api', json=body)
            self.assertEqual(saved.status_code, 200); self.assertEqual(saved.json()['revision'], 1)
            conflict=await client.put('/app/usage-ledger/settings/api', json=body)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual((await client.get('/app/sessions/unknown/usage-ledger')).status_code, 400)
        self.assertTrue(all(c.is_closed for c in self.clients))
        self.assertTrue(all(o['trust_env'] is False and o['follow_redirects'] is False for o in self.client_options))
        self.assertTrue(all(c.url.path.startswith('/loop/') for c in self.calls))

    async def test_auth_failure_never_contacts_loop_and_does_not_cache(self):
        async with self.make() as client:
            client.headers.pop('authorization')
            response=await client.get('/app/sessions/a/usage-ledger')
            self.assertEqual(response.status_code, 401); self.assertIn('no-store', response.headers['cache-control'])
        self.assertEqual(self.calls, []); self.assertEqual(self.clients, [])

    async def test_invalid_query_duplicates_or_setting_body_never_reach_loop(self):
        async with self.make() as client:
            for suffix in ('?mode=api&mode=codex', '?url=https://private.invalid', '?scope=unknown', '?format=json'):
                self.assertEqual((await client.get('/app/sessions/a/usage-ledger'+suffix)).status_code, 400)
            self.assertEqual((await client.get('/app/sessions/a/usage-ledger/records?limit=0')).status_code, 400)
            self.assertEqual((await client.get('/app/sessions/a/usage-ledger/export?format=html')).status_code, 400)
            self.assertEqual((await client.put('/app/usage-ledger/settings/api', content=b'x'*8193)).status_code, 413)
            self.assertEqual((await client.put('/app/usage-ledger/settings/api', content=b'{"revision":0,"revision":1,"policy":{}}')).status_code, 400)
        self.assertEqual(self.calls, [])

    async def test_redirect_and_private_error_body_headers_are_never_forwarded(self):
        async with self.make(lambda r: httpx.Response(302, text='synthetic-private-body', headers={'Location':'https://external.invalid', 'Set-Cookie':'synthetic-private'})) as client:
            response=await client.get('/app/sessions/a/usage-ledger')
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('synthetic-private', response.text)
            self.assertNotIn('set-cookie', response.headers); self.assertNotIn('location', response.headers)
        self.assertEqual(len(self.calls), 1)

    async def test_timeout_and_transport_error_are_redacted_without_retries(self):
        for cls, status in ((httpx.ReadTimeout, 504), (httpx.ConnectError, 502)):
            def fail(request): raise cls('synthetic-private-route-and-key')
            async with self.make(fail) as client:
                response=await client.get('/app/sessions/a/usage-ledger')
                self.assertEqual(response.status_code, status); self.assertNotIn('synthetic-private', response.text)
            self.assertEqual(len(self.calls), 1); self.assertTrue(self.clients[0].is_closed)

    async def test_wrong_session_mode_or_content_type_is_rejected(self):
        cases=[httpx.Response(200, json={'ok':True,'contract_version':1,'mode':'api','scope':'session','session_id':'other','summary':{}}),
               httpx.Response(200, json={'ok':True,'contract_version':1,'mode':'codex','scope':'session','session_id':'a','summary':{}}),
               httpx.Response(200, text='synthetic-private-html', headers={'Content-Type':'text/html'})]
        for case in cases:
            async with self.make(lambda r: case) as client:
                self.assertEqual((await client.get('/app/sessions/a/usage-ledger?mode=api')).status_code, 502)

    async def test_non_export_json_is_bounded_and_closes_upstream(self):
        async with self.make(lambda r: httpx.Response(200, json={'ok':True,'padding':'x'*100})) as client:
            with patch('backend.usage_ledger_proxy.JSON_LIMIT', 32):
                self.assertEqual((await client.get('/app/sessions/a/usage-ledger')).status_code, 502)
        self.assertTrue(self.clients[0].is_closed)

    async def export_response(self, stream):
        client=self.make(lambda r: httpx.Response(200, stream=stream, headers={'Content-Type':'application/json'}))
        self.addAsyncCleanup(client.aclose)
        route=next(r for r in self.app.routes if getattr(r, 'path', '').endswith('/usage-ledger/export'))
        request=Request({'type':'http','method':'GET','path':'/app/sessions/a/usage-ledger/export','query_string':b'format=json',
                         'headers':[(b'authorization',b'Bearer synthetic-browser-only')]})
        return await route.endpoint(sid='a', request=request)

    async def test_export_is_lazy_and_disconnect_closes_upstream_and_client(self):
        stream=TraceStream(); response=await self.export_response(stream)
        self.assertEqual(stream.reads, 0)
        self.assertEqual(len(await anext(response.body_iterator)), 65536); self.assertEqual(stream.reads, 1)
        await response.body_iterator.aclose(); await response.background()
        self.assertEqual(stream.closes, 1); self.assertTrue(self.clients[0].is_closed)

    async def test_unstarted_stream_background_cleanup_releases_resources(self):
        stream=TraceStream(); response=await self.export_response(stream)
        await response.background()
        self.assertEqual(stream.reads, 0); self.assertEqual(stream.closes, 1)
        self.assertTrue(self.clients[0].is_closed)

    async def test_header_send_disconnect_closes_stream_before_first_body_chunk(self):
        stream=TraceStream(); response=await self.export_response(stream)
        async def receive(): return {'type':'http.disconnect'}
        async def send(message): raise OSError('synthetic-disconnected-client')
        with self.assertRaises(Exception):
            await response({'type':'http','method':'GET','asgi':{'spec_version':'2.4'}}, receive, send)
        self.assertEqual(stream.reads, 0); self.assertEqual(stream.closes, 1)
        self.assertTrue(self.clients[0].is_closed)

    async def test_mid_stream_failure_aborts_with_fixed_error_instead_of_fake_completion(self):
        stream=TraceStream(fail=True); response=await self.export_response(stream)
        await anext(response.body_iterator)
        with self.assertRaisesRegex(RuntimeError, '^ledger_proxy_stream_interrupted$'): await anext(response.body_iterator)
        self.assertEqual(stream.closes, 1); self.assertTrue(self.clients[0].is_closed)

    def test_route_collision_with_different_parameter_name_fails_before_mount(self):
        app=FastAPI()
        @app.get('/app/sessions/{session_id}/usage-ledger')
        def existing(session_id): return {}
        count=len(app.routes)
        with self.assertRaisesRegex(LedgerError, 'route_conflict'):
            install(app, enabled=True, check_auth=lambda r: None, base_url='http://127.0.0.1:3020', internal_token=TOKEN)
        self.assertEqual(len(app.routes), count)
