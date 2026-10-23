import base64
import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from backend import web_mcp_oauth as oauth
from backend import web_mcp_oauth_http as net
from backend import web_mcp_credentials as vault
from backend import web_mcp_settings as settings

INFO = {'issuer': 'https://auth.example/tenant', 'resource': 'https://example.com/mcp',
        'authorization_endpoint': 'https://auth.example/authorize', 'token_endpoint': 'https://auth.example/token',
        'iss_required': True, 'cimd': True, 'scopes': ['read']}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'relay.db'
        self.plugin = {'id': 'reader', 'name': 'Reader', 'serverId': 'reader', 'transport': 'streamable-http',
                       'enabled': True, 'url': INFO['resource'], 'options': {}}
        settings.save(self.path, {'revision': 0, 'plugins': [self.plugin]})

    def start(self, **kwargs):
        body = {'revision': 1, 'credential_revision': vault.read(self.path, 'reader')['credential_revision'], **kwargs}
        return oauth.start(self.path, 'reader', body, discover=lambda _: {**INFO, 'scopes': ['read']})

    def complete(self, flow, **kwargs):
        self.http = Mock(return_value=(200, {'token_type': 'Bearer', 'access_token': 'synthetic-access',
                                          'refresh_token': 'synthetic-refresh', 'expires_in': 3600}, ''))
        return oauth.complete(self.path, {'state': flow['state'], 'iss': INFO['issuer'], 'code': 'synthetic-code', **kwargs}, http=self.http)

    def test_pkce_resource_issuer_encrypted_one_use_and_write_only(self):
        flow = self.start(); query = parse_qs(urlsplit(flow['authorization_url']).query)
        self.assertEqual(query['resource'], [INFO['resource']]); self.assertEqual(query['code_challenge_method'], ['S256'])
        result = self.complete(flow)
        form = self.http.call_args.kwargs['form']
        self.assertEqual(query['code_challenge'], [base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode()).digest()).decode().rstrip('=')])
        self.assertEqual(form['resource'], INFO['resource']); self.assertEqual(form['redirect_uri'], oauth.REDIRECT_URI)
        self.assertTrue(result['oauth']['connected']); self.assertEqual(result['credential_revision'], 1)
        for secret in ('synthetic-access', 'synthetic-refresh', form['code_verifier'], flow['state']):
            self.assertNotIn(secret.encode(), self.path.read_bytes()); self.assertNotIn(secret, json.dumps(result))
        with self.assertRaisesRegex(settings.SettingsError, 'flow_expired'): self.complete(flow)
        self.assertEqual(self.http.call_count, 0)

    def test_callback_issuer_is_exact_including_errors_and_consumes_flow(self):
        for value in ('https://AUTH.example/tenant', INFO['issuer']+'/', None):
            flow = self.start()
            with self.assertRaisesRegex(settings.SettingsError, 'issuer_mismatch'): self.complete(flow, iss=value, error='attacker-description')
            self.assertEqual(self.http.call_count, 0)
        flow = self.start()
        with self.assertRaisesRegex(settings.SettingsError, 'authorization_denied'): self.complete(flow, error='access_denied')
        self.assertEqual(self.http.call_count, 0)

    def test_optional_issuer_absent_allowed_present_mismatch_rejected(self):
        for issuer in (None, 'https://wrong.example'):
            flow = oauth.start(self.path, 'reader', {'revision': 1, 'credential_revision': 0}, discover=lambda _: {**INFO, 'iss_required': False})
            body = {'state': flow['state'], 'code': 'code'}
            if issuer: body['iss'] = issuer
            http = Mock(return_value=(200, {'token_type': 'bearer', 'access_token': 'access'}, ''))
            if issuer:
                with self.assertRaisesRegex(settings.SettingsError, 'issuer_mismatch'): oauth.complete(self.path, body, http=http)
                http.assert_not_called()
            else:
                oauth.complete(self.path, body, http=http)
                oauth.logout(self.path, 'reader', {'revision': 1, 'credential_revision': 1})
                # Fresh isolated binding for the next subcase.
                with closing(vault.connection(self.path)) as conn:
                    conn.execute('DELETE FROM web_mcp_credentials'); conn.commit()

    def test_config_or_credentials_changes_invalidate_callback_before_exchange(self):
        flow = self.start()
        vault.write(self.path, 'reader', {'revision': 1, 'credential_revision': 0, 'bearer_token': 'manual'})
        with self.assertRaisesRegex(settings.SettingsError, 'revision_conflict'): self.complete(flow)
        self.http.assert_not_called()
        flow = self.start()
        settings.save(self.path, {'revision': 1, 'plugins': [{**self.plugin, 'url': 'https://different.example/mcp'}]})
        with self.assertRaisesRegex(settings.SettingsError, 'revision_conflict'): self.complete(flow)
        self.http.assert_not_called()

    def test_logout_during_exchange_never_restores_credentials(self):
        flow = self.start()
        def exchange(*args, **kwargs):
            oauth.logout(self.path, 'reader', {'revision': 1, 'credential_revision': 0})
            return 200, {'token_type': 'Bearer', 'access_token': 'access'}, ''
        with self.assertRaisesRegex(settings.SettingsError, 'revision_conflict'):
            oauth.complete(self.path, {'state': flow['state'], 'iss': INFO['issuer'], 'code': 'code'}, http=exchange)
        self.assertFalse(vault.read(self.path, 'reader')['configured'])

    def test_logout_preserves_custom_headers_and_cancels_pending_flow(self):
        vault.write(self.path, 'reader', {'revision': 1, 'credential_revision': 0, 'http_headers': {'x-api-key': 'synthetic-header'}})
        self.complete(self.start()); flow = self.start()
        result = oauth.logout(self.path, 'reader', {'revision': 1, 'credential_revision': 2})
        self.assertIsNone(result['oauth']); self.assertFalse(result['bearer_configured']); self.assertEqual(result['header_names'], ['x-api-key'])
        with self.assertRaisesRegex(settings.SettingsError, 'flow_expired'): self.complete(flow)

    def test_manual_header_edit_preserves_oauth_bearer_edit_disconnects(self):
        self.complete(self.start())
        result = vault.write(self.path, 'reader', {'revision': 1, 'credential_revision': 1, 'http_headers': {'x-api-key': 'header'}})
        self.assertTrue(result['oauth']['connected'])
        result = vault.write(self.path, 'reader', {'revision': 1, 'credential_revision': 2, 'bearer_token': 'manual'})
        self.assertIsNone(result['oauth'])

    def expired(self):
        self.complete(self.start())
        with closing(vault.connection(self.path)) as conn:
            auth = vault.decode_full(self.path, self.plugin, vault.row_for(conn, 'reader'))
            auth['oauth']['expires_at'] = 1
            oauth.save_auth(conn, self.path, self.plugin, auth, 1); conn.commit()

    def test_refresh_rotates_once_keeps_grant_revision_and_never_replays_uncertain(self):
        self.expired()
        http = Mock(return_value=(200, {'token_type': 'bearer', 'access_token': 'rotated', 'refresh_token': 'rotated-refresh', 'expires_in': 3600}, ''))
        oauth.refresh_if_needed(self.path, self.plugin, 1, http=http)
        oauth.refresh_if_needed(self.path, self.plugin, 1, http=http)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.kwargs['form']['resource'], INFO['resource'])
        auth, revision = vault.load_for_request(self.path, self.plugin, 1)
        self.assertEqual(auth['bearer_token'], 'rotated'); self.assertEqual(revision, 1)
        with closing(vault.connection(self.path)) as conn:
            full = vault.decode_full(self.path, self.plugin, vault.row_for(conn, 'reader')); full['oauth']['expires_at'] = 1
            oauth.save_auth(conn, self.path, self.plugin, full, 1); conn.commit()
        http = Mock(side_effect=TimeoutError())
        for _ in range(2):
            with self.assertRaisesRegex(settings.SettingsError, 'reauthorization_required'): oauth.refresh_if_needed(self.path, self.plugin, 1, http=http)
        self.assertEqual(http.call_count, 1)
        self.assertTrue(vault.read(self.path, 'reader')['oauth']['reauthorization_required'])

    def test_concurrent_refresh_and_logout_cannot_reintroduce_tokens(self):
        self.expired()
        def http(*args, **kwargs):
            with self.assertRaisesRegex(settings.SettingsError, 'reauthorization_required'):
                oauth.refresh_if_needed(self.path, self.plugin, 1, http=Mock())
            oauth.logout(self.path, 'reader', {'revision': 1, 'credential_revision': 1})
            return 200, {'token_type': 'Bearer', 'access_token': 'rotated'}, ''
        with self.assertRaisesRegex(settings.SettingsError, 'reauthorization_required'): oauth.refresh_if_needed(self.path, self.plugin, 1, http=http)
        self.assertFalse(vault.read(self.path, 'reader')['configured'])

    def test_public_registration_requires_matching_issuer(self):
        for body in ({}, {'client_id': 'registered', 'issuer': 'https://wrong.example'}):
            with self.assertRaises(settings.SettingsError):
                oauth.start(self.path, 'reader', {'revision': 1, 'credential_revision': 0, **body}, discover=lambda _: {**INFO, 'cimd': False})
        flow = self.start(client_id='registered', issuer=INFO['issuer'])
        self.assertEqual(parse_qs(urlsplit(flow['authorization_url']).query)['client_id'], ['registered'])

    def test_new_login_replaces_old_and_expired_flow_is_deleted(self):
        old = self.start(); new = self.start()
        with self.assertRaisesRegex(settings.SettingsError, 'flow_expired'): self.complete(old)
        with closing(oauth.connection(self.path)) as conn:
            conn.execute('UPDATE web_mcp_oauth_flows SET expires=0'); conn.commit()
        with self.assertRaisesRegex(settings.SettingsError, 'flow_expired'): self.complete(new)


class DiscoveryTests(unittest.TestCase):
    def metadata(self):
        return {'issuer': INFO['issuer'], 'authorization_endpoint': INFO['authorization_endpoint'], 'token_endpoint': INFO['token_endpoint'],
                'code_challenge_methods_supported': ['S256'], 'response_types_supported': ['code'],
                'token_endpoint_auth_methods_supported': ['none'], 'client_id_metadata_document_supported': True}

    def test_challenge_priority_and_scopes(self):
        http = Mock(side_effect=[(401, None, 'Basic realm="other", Bearer resource_metadata="https://meta.example/resource", scope="read write"'),
                                (200, {'resource': INFO['resource'], 'authorization_servers': [INFO['issuer']], 'scopes_supported': ['ignored']}, ''),
                                (200, self.metadata(), '')])
        value = net.discover(INFO['resource'], http=http)
        self.assertEqual(http.call_args_list[1].args[0], 'https://meta.example/resource'); self.assertEqual(value['scopes'], ['read', 'write'])

    def test_well_known_and_oidc_fallback_order(self):
        http = Mock(side_effect=[(401, None, ''), (404, None, ''),
                                (200, {'resource': INFO['resource'], 'authorization_servers': [INFO['issuer']]}, ''),
                                (404, None, ''), (404, None, ''), (200, self.metadata(), '')])
        net.discover(INFO['resource'], http=http)
        self.assertEqual([c.args[0] for c in http.call_args_list], [INFO['resource'],
                         'https://example.com/.well-known/oauth-protected-resource/mcp', 'https://example.com/.well-known/oauth-protected-resource',
                         'https://auth.example/.well-known/oauth-authorization-server/tenant',
                         'https://auth.example/.well-known/openid-configuration/tenant', 'https://auth.example/tenant/.well-known/openid-configuration'])

    def test_resource_issuer_pkce_and_bad_metadata_rejected(self):
        for changed in ({'issuer': INFO['issuer']+'/'}, {'code_challenge_methods_supported': ['plain']}, {'token_endpoint': 'http://local/token'},
                        {'token_endpoint_auth_methods_supported': ['client_secret_basic']}, {'code_challenge_methods_supported': 'S256'}):
            http = Mock(side_effect=[(401, None, ''), (200, {'resource': INFO['resource'], 'authorization_servers': [INFO['issuer']]}, ''),
                                    (200, {**self.metadata(), **changed}, '')])
            with self.subTest(changed=changed), self.assertRaises(settings.SettingsError): net.discover(INFO['resource'], http=http)

    def test_unsafe_urls_and_duplicate_challenge_parameters_rejected(self):
        for value in ('http://example.com', 'https://u:p@example.com', 'https://example.com/#x', 'https://example.com:444/mcp', 'https://example.com/\\x', 'https://example.com/\n'):
            with self.subTest(value=value), self.assertRaises(settings.SettingsError): net.url(value)
        with self.assertRaises(settings.SettingsError): net.challenge_parameters('Bearer scope="read", scope="write"')

    def test_pinned_transport_blocks_private_dns_and_redirects(self):
        with patch.object(net.wire.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), patch.object(net.wire, 'PinnedHTTPS') as conn:
            with self.assertRaises(net.wire.InspectionError): net.request('https://example.com', time.monotonic()+2)
            conn.assert_not_called()
        connection = Mock(); connection.getresponse.return_value.status = 302
        with patch.object(net.wire, 'resolve', return_value=(urlsplit('https://example.com'), '8.8.8.8')), patch.object(net.wire, 'PinnedHTTPS', return_value=connection):
            with self.assertRaisesRegex(settings.SettingsError, 'redirect_not_allowed'): net.request('https://example.com', time.monotonic()+2)
        self.assertEqual(connection.request.call_count, 1); connection.close.assert_called_once()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_public_mutations_authenticated_bounded_and_no_store(self):
        import httpx
        from fastapi import FastAPI, HTTPException
        from types import SimpleNamespace
        from backend.web_mcp_settings_routes import install
        def auth(request):
            if request.headers.get('Authorization') != 'Bearer synthetic-relay': raise HTTPException(401)
        with tempfile.TemporaryDirectory() as tmp:
            relay = SimpleNamespace(app=FastAPI(), DB_PATH=Path(tmp)/'relay.db', check_auth=auth)
            install(relay)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app), base_url='https://relay.example') as client:
                response = await client.get('/app/mcp/oauth/client-metadata.json')
                self.assertEqual(response.status_code, 200); self.assertEqual(response.json()['redirect_uris'], [oauth.REDIRECT_URI])
                for path in ('/app/mcp/plugins/reader/oauth/start', '/app/mcp/oauth/complete', '/app/mcp/plugins/reader/oauth/logout'):
                    self.assertEqual((await client.post(path, json={})).status_code, 401)
                    response = await client.post(path, json={}, headers={'Authorization': 'Bearer synthetic-relay'})
                    self.assertEqual(response.status_code, 400); self.assertEqual(response.headers['cache-control'], 'private, no-store')
                    response = await client.post(path, content='x'*16385, headers={'Authorization': 'Bearer synthetic-relay'})
                    self.assertEqual(response.status_code, 413)


if __name__ == '__main__': unittest.main()
