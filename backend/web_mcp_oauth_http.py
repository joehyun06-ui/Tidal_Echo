"""Bounded, credential-free OAuth discovery and pinned HTTPS token requests."""
import json
import re
import socket
import threading
import time
from functools import wraps
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import parse_http_list

from . import web_mcp_inspection as wire
from .web_mcp_settings import SettingsError

LIMIT = 131072
SLOTS = threading.BoundedSemaphore(2)


def bounded(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        if not SLOTS.acquire(blocking=False): fail('mcp_oauth_busy')
        try: return operation(*args, **kwargs)
        finally: SLOTS.release()
    return run


def fail(category='mcp_oauth_metadata_invalid'):
    raise SettingsError(category)


def url(value, *, query=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or '\\' in value:
        fail()
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.port not in (None, 443) or p.username is not None or p.password is not None or '#' in value or not query and '?' in value:
            fail()
    except ValueError:
        fail()
    return p


def canonical(value):
    p = url(value)
    return urlunsplit(('https', p.hostname.lower(), p.path.rstrip('/') if p.path == '/' else p.path, '', ''))


@bounded
def request(target, deadline, *, form=None, probe=False):
    """No cookies, redirects, proxies or credentials inherited from the relay."""
    p = url(target, query=True)
    _, address = wire.resolve(target)
    conn = wire.PinnedHTTPS(p.hostname, address, wire.remaining(deadline))
    timer = None
    try:
        conn.connect()
        sock = conn.sock
        def expire():
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        timer = threading.Timer(wire.remaining(deadline), expire)
        timer.daemon = True; timer.start()
        headers = {'Accept': 'application/json', 'Connection': 'close'}
        body = None
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urlencode(form).encode()
        conn.request('POST' if form is not None else 'GET', p.path + ('?' + p.query if p.query else '') or '/', body=body, headers=headers)
        response = conn.getresponse()
        if 300 <= response.status < 400:
            fail('mcp_oauth_redirect_not_allowed')
        challenge = ', '.join(v for k, v in response.getheaders() if k.lower() == 'www-authenticate')
        if len(challenge) > 16384: fail()
        if probe:
            return response.status, None, challenge
        chunks, size = [], 0
        while True:
            sock.settimeout(wire.remaining(deadline))
            chunk = response.read1(min(16384, LIMIT + 1 - size))
            if not chunk: break
            size += len(chunk); chunks.append(chunk)
            if size > LIMIT: fail()
        if response.status in (404, 405): return response.status, None, challenge
        if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json': fail()
        try: value = json.loads(b''.join(chunks))
        except (ValueError, UnicodeError): fail()
        if not isinstance(value, dict): fail()
        return response.status, value, challenge
    finally:
        if timer: timer.cancel()
        conn.close()


def challenge_parameters(header):
    """Select a Bearer challenge, including combined/multiple challenge headers."""
    result, active, seen = {}, False, False
    for part in parse_http_list(header):
        scheme = re.match(r'^([A-Za-z][A-Za-z0-9_-]*)\s+(.*)$', part)
        if scheme:
            active = scheme[1].lower() == 'bearer'
            if active:
                if seen: fail()
                seen = True
            part = scheme[2]
        elif part.lower() == 'bearer':
            if seen: fail()
            seen = active = True
            continue
        if not active: continue
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', part)
        if not match: fail()
        key = match[1].lower()
        if key in result: fail()
        result[key] = re.sub(r'\\(.)', r'\1', match[2]) if match[2] is not None else match[3]
    return result


def scopes(value):
    if value is None: return []
    if isinstance(value, str): value = value.split(' ')
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(s, str) or not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]{1,256}', s) for s in value): fail()
    return list(dict.fromkeys(value))


def discover(resource, *, http=request):
    resource = canonical(resource)
    p = url(resource); origin = 'https://' + p.netloc
    deadline = time.monotonic() + 12
    status, _, header = http(resource, deadline, probe=True)
    params = challenge_parameters(header) if status == 401 else {}
    targets = [params['resource_metadata']] if 'resource_metadata' in params else list(dict.fromkeys([
        origin + '/.well-known/oauth-protected-resource' + p.path,
        origin + '/.well-known/oauth-protected-resource']))
    protected = None
    for target in targets:
        url(target)
        status, doc, _ = http(target, deadline)
        if status in (404, 405): continue
        if status != 200: fail()
        protected = doc; break
    if not isinstance(protected, dict) or canonical(protected.get('resource')) != resource: fail()
    issuers = protected.get('authorization_servers')
    if not isinstance(issuers, list) or not 1 <= len(issuers) <= 10: fail()
    # Deterministic first issuer; never send one issuer's registration to another.
    issuer = issuers[0]; p = url(issuer); origin = 'https://' + p.netloc
    targets = list(dict.fromkeys([
        origin + '/.well-known/oauth-authorization-server' + p.path,
        origin + '/.well-known/openid-configuration' + p.path,
        issuer.rstrip('/') + '/.well-known/openid-configuration']))
    metadata = None
    for target in targets:
        status, doc, _ = http(target, deadline)
        if status in (404, 405): continue
        if status != 200 or not isinstance(doc, dict) or doc.get('issuer') != issuer: fail()
        metadata = doc; break
    if metadata is None: fail()
    for key in ('authorization_endpoint', 'token_endpoint'): url(metadata.get(key), query=True)
    for key in ('code_challenge_methods_supported', 'response_types_supported', 'token_endpoint_auth_methods_supported'):
        if key in metadata and (not isinstance(metadata[key], list) or any(not isinstance(v, str) for v in metadata[key])): fail()
    if 'S256' not in metadata.get('code_challenge_methods_supported', []): fail('mcp_oauth_pkce_required')
    if 'code' not in metadata.get('response_types_supported', []): fail()
    if 'none' not in metadata.get('token_endpoint_auth_methods_supported', ['client_secret_basic']):
        fail('mcp_oauth_public_client_required')
    if type(metadata.get('authorization_response_iss_parameter_supported', False)) is not bool: fail()
    requested = scopes(params.get('scope', protected.get('scopes_supported')))
    return {'issuer': issuer, 'resource': resource, 'authorization_endpoint': metadata['authorization_endpoint'],
            'token_endpoint': metadata['token_endpoint'], 'iss_required': metadata.get('authorization_response_iss_parameter_supported', False),
            'cimd': metadata.get('client_id_metadata_document_supported') is True, 'scopes': requested}
