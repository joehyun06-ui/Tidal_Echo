"""Synthetic browser auth, actual internal status route and canonical journal.

No app bootstrap, real login, model API or external network. The proxy reaches
the existing HTTP handler through ASGITransport with its original token guard.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

from backend.g3_projection_contract import AccessScope
from backend.session_lifecycle_status_proxy import install, PATH, TrustedStatusContext
from backend.session_window_activation_http import install as install_internal
from backend.session_window_activation_http import PATH as INTERNAL_PATH
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_activation_http import fixture
from backend.tests.test_session_lifecycle_history_http import original_auth, TOKEN

ORIGIN = 'https://frontend.synthetic.invalid'
BROWSER = 'Bearer synthetic-browser-credential'


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, hook=None):
        self.chunks, self.hook, self.closed = chunks, hook, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.hook:
                await self.hook()
            yield chunk

    async def aclose(self):
        self.closed = True


@unittest.skipUnless(os.name == 'posix', 'existing private canonical JSON fixture is POSIX')
class StatusProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.h, self.journal, self.body = fixture(Path(directory.name) / 'canonical.sqlite')
        self.addCleanup(self.h.conn.close)
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline only')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('offline only')))
        self.context = TrustedStatusContext(CTX, CID, 'login:1', 'version:1')
        self.denied = False; self.auth_calls = 0; self.sent = []; self.factory_options = []
        self.internal = FastAPI(); check = original_auth()
        def internal_auth(request):
            check(request); return CTX
        install_internal(self.internal, self.journal, authenticate=internal_auth, enabled=True)
        self.transport = httpx.ASGITransport(app=self.internal)
        self.app = FastAPI()
        install(self.app, enabled=True, authenticate=self.authenticate,
                expected_access=CTX, expected_origin=ORIGIN, base_url='http://127.0.0.1:3020',
                internal_token=TOKEN, client_factory=self.factory)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url='https://relay.synthetic.invalid', headers={
                'Authorization': BROWSER, 'Origin': ORIGIN, 'Content-Type': 'application/json'})
        self.addAsyncCleanup(self.client.aclose)
        self.path = PATH.replace('{conversation_id}', CID)

    def authenticate(self, request, cid):
        self.auth_calls += 1
        if request.headers.getlist('authorization') != [BROWSER]:
            raise HTTPException(401)
        if self.denied:
            raise HTTPException(403)
        return self.context

    def factory(self, **options):
        self.factory_options.append(options)
        async def record(request):
            self.sent.append(request)
        return httpx.AsyncClient(transport=self.transport,
                                 event_hooks={'request': [record]}, **options)

    def private(self, result):
        self.assertEqual(result.headers['cache-control'], 'private, no-store')
        self.assertEqual(result.headers['referrer-policy'], 'no-referrer')
        self.assertEqual(result.headers['x-content-type-options'], 'nosniff')
        for private in (TOKEN, BROWSER, str(self.h.config_path), 'raw private error'):
            self.assertNotIn(private, result.text)

    def response(self, body=None, *, status=200, headers=None, chunks=None, hook=None):
        value = body if body is not None else {'ok': True, 'state': 'attempt_missing',
            'historical': False, 'revision_applied': None, 'window_confirmed_at_apply': False,
            'current_window_confirmed': False, 'reply_completed': False, 'retry_authorized': False}
        stream = TrackedStream(chunks if chunks is not None else [json.dumps(value).encode()], hook)
        self.transport = httpx.MockTransport(lambda _: httpx.Response(status,
            headers=headers or {'content-type': 'application/json'}, stream=stream))
        return stream

    async def test_default_off_does_not_inspect_dependencies(self):
        self.assertFalse(install(None))
        self.assertFalse(install(None, authenticate=object(), base_url=object(), internal_token=object()))

    async def test_pinned_configuration_and_route_conflict_fail_before_io(self):
        common = dict(enabled=True, authenticate=self.authenticate, expected_access=CTX,
            expected_origin=ORIGIN, base_url='http://127.0.0.1:3020', internal_token=TOKEN)
        for change in ({'base_url':'https://example.com'}, {'base_url':'http://localhost:3020'},
            {'base_url':'http://127.0.0.1:3020/elsewhere'}, {'base_url':'http://user@127.0.0.1:3020'},
            {'base_url':'http://127.0.0.1:3020?target=x'}, {'internal_token':'bad\nvalue'},
            {'expected_access':True}, {'expected_origin':'https://frontend.synthetic.invalid/'},
            {'expected_origin':'http://public.invalid'}, {'authenticate':None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                install(FastAPI(), **{**common, **change})
        with self.assertRaisesRegex(ValueError, 'route_conflict'):
            install(self.app, **common)
        alias = FastAPI()
        alias.add_api_route(PATH.replace('{conversation_id}', '{sid}'), lambda: {}, methods=['POST'])
        with self.assertRaisesRegex(ValueError, 'route_conflict'):
            install(alias, **common)
        self.assertEqual(self.sent, [])

    async def test_actual_three_states_are_read_only_and_rechecked(self):
        for state in ('attempt_missing', 'unconfirmed', 'historical_confirmed'):
            if state == 'unconfirmed':
                self.journal.record_attempt(CID, 'attempt_1', packet_sha256=self.body['packet_sha256'],
                    expected_revision=self.h.record().revision, access=CTX)
            elif state == 'historical_confirmed':
                self.h.confirm()
                activate_recorded_attempt(self.h.engine, self.journal, CID, 'activate_1',
                    attempt_request_id='attempt_1', packet_sha256=self.body['packet_sha256'], access=CTX, enabled=True)
            before = list(self.h.conn.iterdump()); config = self.h.config_path.read_bytes()
            count = self.auth_calls
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['state'], state)
            self.assertFalse(result.json()['current_window_confirmed'])
            self.assertFalse(result.json()['reply_completed']); self.assertFalse(result.json()['retry_authorized'])
            self.assertEqual(self.auth_calls-count, 3)
            self.assertEqual(before, list(self.h.conn.iterdump()))
            self.assertEqual(config, self.h.config_path.read_bytes()); self.private(result)

    async def test_browser_headers_are_not_forwarded_and_options_disable_redirects(self):
        result = await self.client.post(self.path, json=self.body,
            headers={'cookie': 'private-cookie', 'x-private-header': 'private'})
        self.assertEqual(result.status_code, 200)
        sent = self.sent[0]
        self.assertEqual(str(sent.url), 'http://127.0.0.1:3020' + INTERNAL_PATH)
        self.assertEqual(sent.method, 'POST'); self.assertEqual(json.loads(sent.content), self.body)
        self.assertEqual(sent.headers['x-api-loop-internal-token'], TOKEN)
        for name in ('authorization', 'cookie', 'origin', 'x-private-header'):
            self.assertNotIn(name, sent.headers)
        self.assertIs(self.factory_options[0]['trust_env'], False)
        self.assertIs(self.factory_options[0]['follow_redirects'], False)

    async def test_unauthorized_precedes_body_read_and_client_construction(self):
        async def forbidden():
            raise AssertionError('body must not be read')
            yield b''
        result = await self.client.post(self.path, content=forbidden(), headers={'authorization':'wrong'})
        self.assertEqual(result.status_code, 401); self.assertEqual(self.sent, [])
        self.assertEqual(self.factory_options, []); self.private(result)

    async def test_shared_secret_callback_is_not_a_scoped_grant(self):
        self.context = None
        result = await self.client.post(self.path, json=self.body)
        self.assertEqual(result.status_code, 503); self.assertEqual(self.sent, [])

    async def test_wrong_scope_owner_or_conversation_cannot_forward(self):
        original = self.context
        for context in (replace(original, access=AccessScope('other_owner', CTX.allowed_scopes)),
                replace(original, access=AccessScope(CTX.principal_id, frozenset({'other_scope'}))),
                replace(original, conversation_id='other_chat')):
            self.context = context
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 403); self.private(result)
        self.assertEqual(self.sent, [])

    async def test_query_origin_duplicate_auth_and_injected_internal_header_refuse(self):
        result = await self.client.post(self.path+'?token=anything', json=self.body)
        self.assertEqual(result.status_code, 400)
        for headers, status in (({'origin':'https://evil.invalid'}, 403),
                ({'x-api-loop-internal-token':TOKEN}, 400),
                ([('authorization',BROWSER)]*2, 401)):
            result = await self.client.post(self.path, json=self.body, headers=headers)
            self.assertEqual(result.status_code, status); self.private(result)
        self.assertEqual(self.sent, [])

    async def test_extra_fields_bad_reference_and_duplicate_json_refuse(self):
        for extra in ({'dispatch':True}, {'principal_id':CTX.principal_id}, {'context_version':'forged'},
                      {'allowed_scopes':list(CTX.allowed_scopes)}, {'conversation_id':'wrong'},
                      {'packet_sha256':'bad'}, {'attempt_request_id':[]}, {'activation_request_id':True}):
            result = await self.client.post(self.path, json={**self.body, **extra})
            self.assertEqual(result.status_code, 400, result.text)
        result = await self.client.post(self.path, content='{"conversation_id":"x","conversation_id":"y"}')
        self.assertEqual(result.status_code, 400); self.assertEqual(self.sent, [])

    async def test_request_size_encoding_and_type_are_bounded(self):
        result = await self.client.post(self.path, content=b'x'*4097)
        self.assertEqual(result.status_code, 413)
        for headers in ({'content-encoding':'gzip'}, {'content-type':'text/plain'}):
            result = await self.client.post(self.path, json=self.body, headers=headers)
            self.assertEqual(result.status_code, 415)
        self.assertEqual(self.sent, [])

    async def test_auth_change_during_body_refuses_before_upstream(self):
        async def body():
            self.context = replace(self.context, auth_epoch='login:2')
            yield json.dumps(self.body).encode()
        result = await self.client.post(self.path, content=body())
        self.assertEqual(result.status_code, 403); self.assertEqual(self.sent, [])

    async def test_epochs_revocation_and_owner_change_during_response_are_denied(self):
        original = self.context
        changes = ({'auth_epoch':'login:2'}, {'context_version':'version:2'},
                   {'access':AccessScope('other_owner',CTX.allowed_scopes)}, {'conversation_id':'other_chat'}, None)
        for change in changes:
            self.context = original; self.denied = False
            async def hook():
                if change is None: self.denied = True
                else: self.context = replace(self.context, **change)
            stream = self.response(hook=hook)
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 403, result.text); self.assertTrue(stream.closed)
            self.assertNotIn('state', result.json()); self.private(result)

    async def test_oversized_chunked_or_declared_response_closes_without_exposure(self):
        for chunks in ([b'x'*4097], [b'x'*1024]*5):
            stream = self.response(chunks=chunks)
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 502); self.assertTrue(stream.closed); self.private(result)
        for length in ('4097', '-1', 'nonnumeric'):
            stream = self.response(headers={'content-type':'application/json','content-length':length})
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code,502);self.assertTrue(stream.closed)

    async def test_invalid_json_encoding_shape_and_overclaims_never_confirm(self):
        for data in (b'raw private error', b'\xff', b'{"ok":true,"ok":false}',
                json.dumps({'ok':True}).encode(),
                json.dumps({'ok':True,'state':{},'historical':False,'revision_applied':None,
                    'window_confirmed_at_apply':False,'current_window_confirmed':False,
                    'reply_completed':False,'retry_authorized':False}).encode()):
            stream = self.response(chunks=[data])
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 502, result.text); self.assertTrue(stream.closed)
        for key in ('current_window_confirmed','reply_completed','retry_authorized'):
            value = {'ok':True,'state':'historical_confirmed','historical':True,'revision_applied':1,
                'window_confirmed_at_apply':True,'current_window_confirmed':False,
                'reply_completed':False,'retry_authorized':False,key:True}
            self.response(value); result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 502)

    async def test_upstream_content_type_encoding_and_redirect_are_not_trusted(self):
        for headers in ({'content-type':'text/html'}, {'content-type':'application/json','content-encoding':'gzip'}):
            stream = self.response(headers=headers)
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, 502); self.assertTrue(stream.closed)
        stream = self.response(status=302, headers={'location':'https://evil.invalid'})
        result = await self.client.post(self.path, json=self.body)
        self.assertEqual(result.status_code, 502); self.assertTrue(stream.closed)
        self.assertEqual(len(self.sent), 3)

    async def test_upstream_error_statuses_are_fixed_and_never_read_raw_body(self):
        for status, expected in ((401,502),(403,403),(409,409),(503,503),(500,502)):
            async def forbidden():
                raise AssertionError('error body must not be read')
            stream = self.response(status=status, chunks=[b'raw private error'], hook=forbidden)
            result = await self.client.post(self.path, json=self.body)
            self.assertEqual(result.status_code, expected, result.text)
            self.assertTrue(stream.closed); self.private(result)

    async def test_total_deadline_closes_slow_stream_and_does_not_retry(self):
        async def slow(): await asyncio.sleep(1)
        stream = self.response(hook=slow)
        with patch('backend.session_lifecycle_status_proxy.TIMEOUT_SECONDS', 0.01):
            result = await self.client.post(self.path, json=self.body)
        self.assertEqual(result.status_code, 503); self.assertTrue(stream.closed)
        self.assertEqual(len(self.sent), 1); self.private(result)

    async def test_async_authentication_callback_is_supported(self):
        self.app = FastAPI()
        async def authenticate(request,cid):
            await asyncio.sleep(0); return self.authenticate(request,cid)
        install(self.app, enabled=True, authenticate=authenticate, expected_access=CTX,
            expected_origin=ORIGIN, base_url='http://127.0.0.1:3020', internal_token=TOKEN, client_factory=self.factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://relay.synthetic.invalid') as client:
            result = await client.post(self.path, json=self.body,headers={
                'authorization':BROWSER,'origin':ORIGIN,'content-type':'application/json'})
        self.assertEqual(result.status_code,200);self.assertEqual(self.auth_calls,3)
