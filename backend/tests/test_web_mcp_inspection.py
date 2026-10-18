import io
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from backend import web_mcp_inspection as mcp
from backend import web_mcp_settings as settings


class ScriptedTransport:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def request(self, message, version, session=None, **kwargs):
        self.calls.append((message, version, session, kwargs))
        if kwargs.get('delete'):
            return 204, None, None
        status, result, token = next(self.replies)
        if isinstance(result, dict):
            result = {'jsonrpc': '2.0', 'id': message.get('id'), **result}
        return status, result, token


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'relay.db'
        self.plugin = {'id': 'reader', 'name': 'Synthetic', 'serverId': 'reader', 'transport': 'streamable-http',
                       'enabled': True, 'url': 'https://example.com/mcp', 'options': {}}
        settings.save(self.path, {'revision': 0, 'plugins': [self.plugin]})

    def run_script(self, replies):
        transport = ScriptedTransport(replies)
        return mcp.discover(self.plugin, lambda *_: transport), transport

    def test_modern_metadata_pagination_and_no_execution(self):
        result, transport = self.run_script([(200, {'result': {'tools': [{'name': 'read', 'description': '<img onerror=bad>'}], 'nextCursor': 'second'}}, None),
                                             (200, {'result': {'tools': [{'name': 'write'}]}}, None)])
        self.assertEqual([t['name'] for t in result['tools']], ['read', 'write'])
        self.assertEqual(result['protocol_version'], mcp.MODERN)
        self.assertFalse(result['truncated'])
        for message, version, _, _ in transport.calls:
            self.assertEqual(message['method'], 'tools/list')
            self.assertEqual(message['params']['_meta']['io.modelcontextprotocol/protocolVersion'], version)
            self.assertEqual(message['params']['_meta']['io.modelcontextprotocol/clientCapabilities'], {})
        self.assertEqual(transport.calls[1][0]['params']['cursor'], 'second')

    def test_legacy_negotiation_session_and_cleanup(self):
        result, transport = self.run_script([(400, None, None), (200, {'result': {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}}}, 'session'),
                                             (202, None, None), (200, {'result': {'tools': []}}, None)])
        self.assertEqual(result['protocol_version'], '2025-06-18')
        self.assertEqual([c[0]['method'] for c in transport.calls[:-1]], ['tools/list', 'initialize', 'notifications/initialized', 'tools/list'])
        self.assertEqual(transport.calls[2][2], 'session')
        self.assertNotIn('id', transport.calls[2][0])
        self.assertTrue(transport.calls[-1][3]['delete'])

    def test_modern_version_error_does_not_blindly_downgrade(self):
        for code in (-32020, -32021, -32022):
            with self.subTest(code=code), self.assertRaises(mcp.InspectionError):
                self.run_script([(400, {'error': {'code': code, 'data': {'supported': ['future']}}}, None)])

    def test_no_credentials_or_process_start(self):
        for change in ({'transport': 'stdio'}, {'transport': 'sse'}, {'options': {'bearer_token_env_var': 'RELAY_SECRET'}}, {'options': {'env_http_headers': {'Authorization': 'RELAY_SECRET'}}}):
            factory = Mock()
            with self.assertRaises(mcp.InspectionError):
                mcp.discover({**self.plugin, **change}, factory)
            factory.assert_not_called()

    def test_dns_rebinding_private_and_mixed_addresses_blocked(self):
        for addresses in (['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'], ['::1'], ['::ffff:8.8.8.8'], ['8.8.8.8', '10.0.0.1'], ['224.0.0.1']):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses]
            with self.subTest(addresses=addresses), patch.object(socket, 'getaddrinfo', return_value=records), self.assertRaises(mcp.InspectionError):
                mcp.resolve(self.plugin['url'])
        for url in ('http://localhost/mcp', 'https://example.com:8443/mcp'):
            with patch.object(socket, 'getaddrinfo') as lookup, self.assertRaises(mcp.InspectionError):
                mcp.resolve(url)
            lookup.assert_not_called()

    def test_pinned_socket_keeps_tls_hostname(self):
        context = Mock(); raw = Mock()
        with patch.object(mcp.ssl, 'create_default_context', return_value=context), patch.object(socket, 'create_connection', return_value=raw) as connect:
            conn = mcp.PinnedHTTPS('example.com', '8.8.8.8', 3); conn.connect()
        connect.assert_called_once_with(('8.8.8.8', 443), 3)
        context.wrap_socket.assert_called_once_with(raw, server_hostname='example.com')

    def test_sse_stops_at_matching_result_and_caps_bytes(self):
        class Response(io.BytesIO):
            status = 200
            def getheader(self, *_): return 'text/event-stream'
        response = Response(b': heartbeat\n\ndata: {"jsonrpc":"2.0","method":"notifications/message"}\n\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n')
        self.assertEqual(mcp.decode_response(response, 1, time.monotonic()+2, Mock())['result'], {'tools': []})
        with self.assertRaisesRegex(mcp.InspectionError, 'response_too_large'):
            mcp.decode_response(Response(b'data: '+b'x'*(mcp.LIMIT+1)), 1, time.monotonic()+2, Mock())

    def test_tool_and_page_caps_and_repeated_cursor(self):
        result, _ = self.run_script([(200, {'result': {'tools': [{'name': str(i)} for i in range(101)]}}, None)])
        self.assertEqual(len(result['tools']), 100); self.assertTrue(result['truncated'])
        with self.assertRaisesRegex(mcp.InspectionError, 'invalid_response'):
            self.run_script([(200, {'result': {'tools': [], 'nextCursor': 'same'}}, None)]*2)
        result, transport = self.run_script([(200, {'result': {'tools': [], 'nextCursor': str(i)}}, None) for i in range(4)])
        self.assertTrue(result['truncated']); self.assertEqual(len(transport.calls), 4)

    def test_revision_rechecked_after_network_and_errors_sanitized(self):
        def changed(_, **kwargs):
            settings.save(self.path, {'revision': 1, 'plugins': []})
            return {'tools': []}
        with patch.object(mcp, 'discover', side_effect=changed), self.assertRaisesRegex(settings.SettingsError, 'revision_conflict'):
            mcp.inspect(self.path, 'reader', {'revision': 1})
        with patch.object(mcp, 'discover') as discover, self.assertRaises(settings.SettingsError):
            mcp.inspect(self.path, 'reader', {'revision': 1})
        discover.assert_not_called()

    def test_failures_safe_and_concurrency_bounded(self):
        with patch.object(mcp, 'discover', side_effect=OSError('secret upstream text')):
            result = mcp.inspect(self.path, 'reader', {'revision': 1})
        self.assertEqual(result['status'], 'connection_failed'); self.assertNotIn('secret', json.dumps(result)); self.assertFalse(result['execution_enabled'])
        with patch.object(mcp, 'SLOTS', threading_slot := Mock()):
            threading_slot.acquire.return_value = False
            with self.assertRaisesRegex(settings.SettingsError, 'busy'):
                mcp.inspect(self.path, 'reader', {'revision': 1})

    def test_transport_never_follows_redirect_or_retries_auth(self):
        for status, expected in [(302,'redirect_not_allowed'),(401,'authentication_required'),(403,'authentication_required'),(429,'rate_limited'),(503,'server_unavailable')]:
            response = Mock(status=status)
            connection = Mock(); connection.getresponse.return_value = response
            with patch.object(mcp, 'resolve', return_value=(mcp.urlsplit(self.plugin['url']), '8.8.8.8')), patch.object(mcp, 'PinnedHTTPS', return_value=connection):
                transport = mcp.Transport(self.plugin['url'], time.monotonic()+2)
                with self.assertRaisesRegex(mcp.InspectionError, expected):
                    transport.request({'jsonrpc':'2.0','id':1,'method':'tools/list'}, mcp.MODERN)
            self.assertEqual(connection.request.call_count, 1)
            headers = connection.request.call_args.kwargs['headers']
            self.assertNotIn('Authorization', headers)
            self.assertEqual(headers['Mcp-Method'], 'tools/list')
            connection.close.assert_called_once()


if __name__ == '__main__': unittest.main()

