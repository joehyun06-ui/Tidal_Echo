"""User-initiated metadata discovery only. No credentials, calls or subprocesses."""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import web_mcp_settings as settings
from . import web_mcp_credentials as credentials

MODERN = '2026-07-28'
LEGACY = ('2025-11-25', '2025-06-18', '2025-03-26')
LIMIT = 524288
SLOTS = threading.BoundedSemaphore(2)


class InspectionError(ValueError):
    pass


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise InspectionError('timeout')
    return value


def resolve(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.port not in (None, 443):
        raise InspectionError('public_https_required')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise InspectionError('connection_failed')
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if not ip.is_global or ip.is_multicast or getattr(ip, 'ipv4_mapped', None):
            raise InspectionError('public_https_required')
    return parsed, addresses[0][4][0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Never re-resolve the hostname after validation. TLS still verifies it.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def decode_response(response, request_id, deadline, sock):
    content_type = response.getheader('Content-Type', '').split(';')[0].strip().lower()
    size = 0
    data = []
    if content_type == 'text/event-stream':
        while True:
            sock.settimeout(remaining(deadline))
            line = response.readline(LIMIT + 1)
            size += len(line)
            if size > LIMIT:
                raise InspectionError('response_too_large')
            if not line:
                raise InspectionError('invalid_response')
            line = line.decode('utf-8').rstrip('\r\n')
            if not line and data:
                message = json.loads('\n'.join(data)); data = []
                if isinstance(message, dict) and message.get('id') == request_id:
                    return message
            elif line.startswith('data:'):
                data.append(line[5:].removeprefix(' '))
    if content_type != 'application/json':
        # Legacy servers may reject a modern probe with an HTML 4xx.
        if 400 <= response.status < 500:
            return None
        raise InspectionError('invalid_response')
    chunks = []
    while True:
        sock.settimeout(remaining(deadline))
        chunk = response.read1(min(16384, LIMIT + 1 - size))
        if not chunk:
            break
        chunks.append(chunk); size += len(chunk)
        if size > LIMIT:
            raise InspectionError('response_too_large')
    return json.loads(b''.join(chunks))


class Transport:
    def __init__(self, url, deadline, bearer_token=None):
        self.parsed, self.address = resolve(url)
        self.deadline = deadline
        self.bearer_token = bearer_token

    def request(self, message, version, session=None, *, delete=False):
        connection = PinnedHTTPS(self.parsed.hostname, self.address, remaining(self.deadline))
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': version, 'Connection': 'close'}
        if message:
            headers['Mcp-Method'] = message['method']
        if session:
            headers['Mcp-Session-Id'] = session
        if self.bearer_token:
            headers['Authorization'] = 'Bearer ' + self.bearer_token
        timer = None
        try:
            connection.connect()
            sock = connection.sock
            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining(self.deadline), expire)
            timer.daemon = True
            timer.start()
            sock.settimeout(remaining(self.deadline))
            connection.request('DELETE' if delete else 'POST', self.parsed.path or '/',
                               body=None if delete else json.dumps(message).encode(), headers=headers)
            sock.settimeout(remaining(self.deadline))
            response = connection.getresponse()
            if delete:
                return response.status, None, None
            if response.status in (401, 403):
                raise InspectionError('authentication_required')
            if response.status == 429:
                raise InspectionError('rate_limited')
            if response.status >= 500:
                raise InspectionError('server_unavailable')
            if 300 <= response.status < 400:
                raise InspectionError('redirect_not_allowed')
            session_id = response.getheader('Mcp-Session-Id')
            if session_id and (len(session_id) > 1024 or any(not 33 <= ord(c) <= 126 for c in session_id)):
                raise InspectionError('invalid_response')
            if 'id' not in message:
                if response.status != 202:
                    raise InspectionError('invalid_response')
                return response.status, None, session_id
            return response.status, decode_response(response, message['id'], self.deadline, sock), session_id
        finally:
            if timer:
                timer.cancel()
            connection.close()


def rpc_result(status, message, request_id):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or message.get('id') != request_id:
        raise InspectionError('invalid_response')
    if 'error' in message:
        raise InspectionError('protocol_error')
    result = message.get('result')
    if status != 200 or not isinstance(result, dict) or result.get('resultType', 'complete') != 'complete':
        raise InspectionError('invalid_response')
    return result


def discover(plugin, transport_factory=Transport, *, bearer_token=None):
    if plugin['transport'] != 'streamable-http':
        raise InspectionError('transport_not_supported')
    if plugin['options'].get('env_http_headers') or plugin['options'].get('bearer_token_env_var') and not bearer_token:
        # Configuration names must never grant access to arbitrary relay environment secrets.
        raise InspectionError('credentials_not_connected')
    deadline = time.monotonic() + min(15, plugin['options'].get('startup_timeout_sec', 15))
    transport = transport_factory(plugin['url'], deadline, bearer_token=credentials.validate_token(bearer_token)) if bearer_token else transport_factory(plugin['url'], deadline)
    version, session, sequence = MODERN, None, 0

    def request(method, params=None, notification=False):
        nonlocal sequence
        remaining(deadline)
        sequence += 1
        params = dict(params or {})
        if version == MODERN:
            params['_meta'] = {'io.modelcontextprotocol/protocolVersion': version,
                              'io.modelcontextprotocol/clientInfo': {'name': 'guiting-inspector', 'version': '1'},
                              'io.modelcontextprotocol/clientCapabilities': {}}
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = sequence
        return transport.request(message, version, session)

    try:
        status, message, _ = request('tools/list')
        error = message.get('error') if isinstance(message, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
        if code == -32022:
            supported = error.get('data', {}).get('supported', []) if isinstance(error.get('data'), dict) else []
            choices = [v for v in LEGACY if isinstance(supported, list) and v in supported]
            if not choices:
                raise InspectionError('protocol_not_supported')
            version = choices[0]
        elif 400 <= status < 500 and code not in (-32020, -32021):
            version = LEGACY[0]
        if version != MODERN:
            status, message, session = request('initialize', {'protocolVersion': version, 'capabilities': {},
                                               'clientInfo': {'name': 'guiting-inspector', 'version': '1'}})
            info = rpc_result(status, message, sequence)
            version = info.get('protocolVersion')
            if version not in LEGACY:
                raise InspectionError('protocol_not_supported')
            request('notifications/initialized', notification=True)
            if not isinstance(info.get('capabilities'), dict):
                raise InspectionError('invalid_response')
            if 'tools' not in info['capabilities']:
                return {'tools': [], 'truncated': False, 'protocol_version': version}
            status, message, _ = request('tools/list')
        tools, names, cursors = [], set(), set()
        for page in range(4):
            result = rpc_result(status, message, sequence)
            values = result.get('tools')
            if not isinstance(values, list):
                raise InspectionError('invalid_response')
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not 0 < len(item['name']) <= 160:
                    raise InspectionError('invalid_response')
                if item['name'] in names:
                    continue
                names.add(item['name'])
                if len(tools) == 100:
                    return {'tools': tools, 'truncated': True, 'protocol_version': version}
                description = item.get('description', '')
                tools.append({'name': item['name'], 'description': description[:1000] if isinstance(description, str) else ''})
            cursor = result.get('nextCursor')
            if cursor is None:
                return {'tools': tools, 'truncated': False, 'protocol_version': version}
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 2048 or cursor in cursors:
                raise InspectionError('invalid_response')
            cursors.add(cursor)
            if page < 3:
                status, message, _ = request('tools/list', {'cursor': cursor})
        return {'tools': tools, 'truncated': True, 'protocol_version': version}
    finally:
        if session:
            try:
                transport.request(None, version, session, delete=True)
            except (InspectionError, OSError, ValueError, http.client.HTTPException):
                pass


def inspect(path, plugin_id, body):
    if not isinstance(body, dict) or set(body) != {'revision'} or type(body['revision']) is not int:
        raise settings.SettingsError('mcp_config_invalid')
    current = settings.read(path)
    if body['revision'] != current['revision']:
        raise settings.SettingsError('mcp_revision_conflict', 409)
    plugin = next((p for p in current['plugins'] if p['id'] == plugin_id), None)
    if plugin is None:
        raise settings.SettingsError('mcp_plugin_not_found', 404)
    if not SLOTS.acquire(blocking=False):
        raise settings.SettingsError('mcp_inspection_busy', 429)
    try:
        result = {'ok': True, 'plugin_id': plugin_id, 'revision': current['revision'], 'execution_enabled': False}
        try:
            bearer_token, credential_revision = credentials.load_for_inspection(path,plugin,current['revision'])
            result.update(discover(plugin,bearer_token=bearer_token), status='reachable')
            if bearer_token:
                for tool in result['tools']:
                    for key in ('name','description'):
                        tool[key] = tool[key].replace(bearer_token,'[redacted]')
        except settings.SettingsError:
            raise
        except InspectionError as exc:
            result.update(status=str(exc), tools=[])
        except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
            result.update(status='connection_failed', tools=[])
        if settings.read(path)['revision'] != current['revision']:
            raise settings.SettingsError('mcp_revision_conflict', 409)
        if credentials.read(path,plugin_id)['credential_revision'] != credential_revision:
            raise settings.SettingsError('mcp_revision_conflict',409)
        result['checked_at'] = datetime.now(timezone.utc).isoformat()
        return result
    finally:
        SLOTS.release()

