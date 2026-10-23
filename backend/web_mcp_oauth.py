"""OAuth public-client grants, encrypted PKCE flows and fail-closed refresh.

No grant or token response is returned to the browser. Network operations never
hold SQLite write locks, and an uncertain token exchange is never replayed.
"""
import base64
import hashlib
import json
import secrets
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import InvalidToken
from . import web_mcp_credentials as vault
from . import web_mcp_oauth_http as net
from .web_mcp_settings import SettingsError

CLIENT_ID = 'https://api.ouoalways.com/relay/app/mcp/oauth/client-metadata.json'
REDIRECT_URI = 'https://www.ouoalways.com/design-preview/mcp-oauth-callback.html'


def client_metadata():
    return {'client_id': CLIENT_ID, 'client_name': '归汀', 'client_uri': 'https://www.ouoalways.com',
            'redirect_uris': [REDIRECT_URI], 'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'token_endpoint_auth_method': 'none', 'application_type': 'web'}


def connection(path):
    conn = vault.connection(path)
    conn.execute('CREATE TABLE IF NOT EXISTS web_mcp_oauth_flows (state_hash TEXT PRIMARY KEY, plugin_id TEXT UNIQUE NOT NULL, expires REAL NOT NULL, ciphertext BLOB NOT NULL)')
    conn.execute('DELETE FROM web_mcp_oauth_flows WHERE expires < ?', (time.time(),))
    conn.commit()
    return conn


def revisions(body):
    if not isinstance(body, dict) or any(type(body.get(k)) is not int or not 0 <= body[k] < 2**53-1 for k in ('revision', 'credential_revision')):
        net.fail('mcp_config_invalid')


def binding(conn, plugin_id, body):
    plugin, _ = vault.plugin_at(conn, plugin_id, body['revision'])
    if plugin['transport'] != 'streamable-http': net.fail('mcp_oauth_transport_unsupported')
    row = vault.row_for(conn, plugin_id)
    if (row[1] if row else 0) != body['credential_revision']:
        raise SettingsError('mcp_revision_conflict', 409)
    return plugin, row


def start(path, plugin_id, body, *, discover=net.discover):
    revisions(body)
    if set(body) - {'revision', 'credential_revision', 'client_id', 'issuer'}: net.fail('mcp_config_invalid')
    with closing(connection(path)) as conn:
        plugin, row = binding(conn, plugin_id, body)
        previous = vault.decode_full(path, plugin, row).get('oauth')
    info = discover(plugin['url'])
    client_id = body.get('client_id')
    if client_id:
        vault.validate_token(client_id)
        if len(client_id) > 2048 or body.get('issuer') != info['issuer']:
            net.fail('mcp_oauth_issuer_mismatch')
    elif previous and previous['issuer'] == info['issuer']:
        client_id = previous['client_id']
    elif info['cimd']:
        client_id = CLIENT_ID
    else:
        net.fail('mcp_oauth_client_registration_required')
    if previous and previous['issuer'] == info['issuer']:
        info['scopes'] = net.scopes(info['scopes'] + previous['scopes'])
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    info.update({'client_id': client_id, 'verifier': verifier, 'plugin_id': plugin_id,
                 'scope_binding': vault.scope(plugin), 'revision': body['revision'], 'credential_revision': body['credential_revision']})
    expires = time.time() + 600
    digest = hashlib.sha256(state.encode()).hexdigest()
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current, _ = binding(conn, plugin_id, body)
        if current != plugin: raise SettingsError('mcp_revision_conflict', 409)
        exists = conn.execute('SELECT 1 FROM web_mcp_credentials WHERE ciphertext IS NOT NULL LIMIT 1').fetchone()
        encrypted = vault.cipher(path, create=not exists).encrypt(json.dumps(info).encode())
        conn.execute('DELETE FROM web_mcp_oauth_flows WHERE plugin_id=?', (plugin_id,))
        conn.execute('INSERT INTO web_mcp_oauth_flows VALUES (?,?,?,?)', (digest, plugin_id, expires, encrypted))
        conn.commit()
    p = urlsplit(info['authorization_endpoint'])
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    protected = {'response_type', 'client_id', 'redirect_uri', 'scope', 'resource', 'state', 'code_challenge', 'code_challenge_method'}
    if set(query) & protected: net.fail()
    query.update({'response_type': 'code', 'client_id': client_id, 'redirect_uri': REDIRECT_URI,
                  'resource': info['resource'], 'state': state, 'code_challenge_method': 'S256',
                  'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')})
    if info['scopes']: query['scope'] = ' '.join(info['scopes'])
    return {'ok': True, 'state': state, 'expires_at': expires, 'issuer': info['issuer'],
            'scopes': info['scopes'], 'authorization_url': urlunsplit(p._replace(query=urlencode(query)))}


def tokens(response, old=None):
    if not isinstance(response, dict) or str(response.get('token_type', '')).lower() != 'bearer': net.fail('mcp_oauth_token_invalid')
    access = vault.validate_token(response.get('access_token'))
    refresh = response.get('refresh_token', (old or {}).get('refresh_token'))
    if refresh is not None: vault.validate_token(refresh)
    expires = response.get('expires_in')
    if expires is not None and (type(expires) not in (int, float) or not 0 < expires <= 31536000): net.fail('mcp_oauth_token_invalid')
    return access, refresh, time.time() + expires if expires is not None else None


def save_auth(conn, path, plugin, auth, revision):
    encrypted = vault.encode(path, conn, plugin, auth)
    conn.execute('INSERT INTO web_mcp_credentials VALUES (?,?,?,?,?) ON CONFLICT(plugin_id) DO UPDATE SET scope=excluded.scope,revision=excluded.revision,ciphertext=excluded.ciphertext,updated_at=excluded.updated_at',
                 (plugin['id'], vault.scope(plugin), revision, encrypted, datetime.now(timezone.utc).isoformat()))
    return encrypted


def complete(path, body, *, http=net.request):
    if not isinstance(body, dict) or set(body) - {'state', 'code', 'iss', 'error'} or not isinstance(body.get('state'), str) or len(body['state']) != 43:
        net.fail('mcp_oauth_callback_invalid')
    digest = hashlib.sha256(body['state'].encode()).hexdigest()
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT expires,ciphertext FROM web_mcp_oauth_flows WHERE state_hash=?', (digest,)).fetchone()
        if not row or row[0] < time.time(): net.fail('mcp_oauth_flow_expired')
        try: info = json.loads(vault.cipher(path).decrypt(row[1]))
        except (InvalidToken, ValueError): net.fail('mcp_oauth_flow_expired')
        # Consume before any exchange. Failed/uncertain exchanges require a new login.
        conn.execute('DELETE FROM web_mcp_oauth_flows WHERE state_hash=?', (digest,)); conn.commit()
        plugin, _ = binding(conn, info['plugin_id'], info)
        if vault.scope(plugin) != info['scope_binding']: net.fail('mcp_oauth_flow_expired')
    if ('iss' in body or info['iss_required']) and body.get('iss') != info['issuer']:
        net.fail('mcp_oauth_issuer_mismatch')
    if 'error' in body: net.fail('mcp_oauth_authorization_denied')
    code = body.get('code'); vault.validate_token(code)
    form = {'grant_type': 'authorization_code', 'code': code, 'code_verifier': info['verifier'],
            'client_id': info['client_id'], 'redirect_uri': REDIRECT_URI, 'resource': info['resource']}
    status, result, _ = http(info['token_endpoint'], time.monotonic()+12, form=form)
    if status != 200: net.fail('mcp_oauth_exchange_failed')
    access, refresh, expiry = tokens(result)
    granted = net.scopes(result.get('scope', info['scopes']))
    if not set(granted) <= set(info['scopes']): net.fail('mcp_oauth_scope_changed')
    oauth = {k: info[k] for k in ('issuer', 'resource', 'client_id', 'token_endpoint')}
    oauth.update({'scopes': granted, 'refresh_token': refresh, 'expires_at': expiry})
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        plugin, row = binding(conn, info['plugin_id'], info)
        if vault.scope(plugin) != info['scope_binding']: net.fail('mcp_oauth_flow_expired')
        auth = vault.decode_full(path, plugin, row)
        auth.update({'bearer_token': access, 'oauth': oauth})
        save_auth(conn, path, plugin, auth, info['credential_revision']+1)
        conn.commit()
        return vault.view(conn, path, plugin['id'])


def logout(path, plugin_id, body):
    revisions(body)
    if set(body) != {'revision', 'credential_revision'}: net.fail('mcp_config_invalid')
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        plugin, row = binding(conn, plugin_id, body)
        auth = vault.decode_full(path, plugin, row)
        if auth.pop('oauth', None): auth['bearer_token'] = None
        save_auth(conn, path, plugin, auth, body['credential_revision']+1)
        conn.execute('DELETE FROM web_mcp_oauth_flows WHERE plugin_id=?', (plugin_id,))
        conn.commit()
        return vault.view(conn, path, plugin_id)


def refresh_if_needed(path, plugin, revision, *, http=net.request):
    with closing(vault.connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        vault.plugin_at(conn, plugin['id'], revision)
        row = vault.row_for(conn, plugin['id']); auth = vault.decode_full(path, plugin, row)
        oauth = auth.get('oauth')
        if not oauth: return
        if oauth.get('refreshing') or oauth.get('failed'): net.fail('mcp_oauth_reauthorization_required')
        if oauth['expires_at'] is None or oauth['expires_at'] > time.time()+30: return
        if not oauth.get('refresh_token'): net.fail('mcp_oauth_reauthorization_required')
        oauth['refreshing'] = True
        claimed = save_auth(conn, path, plugin, auth, row[1]); conn.commit()
    try:
        status, result, _ = http(oauth['token_endpoint'], time.monotonic()+12, form={
            'grant_type': 'refresh_token', 'refresh_token': oauth['refresh_token'],
            'client_id': oauth['client_id'], 'resource': oauth['resource']})
        if status != 200: net.fail('mcp_oauth_reauthorization_required')
        access, refresh, expiry = tokens(result, oauth)
        granted = net.scopes(result.get('scope', oauth['scopes']))
        if set(granted) != set(oauth['scopes']): net.fail('mcp_oauth_scope_changed')
        oauth.update({'refresh_token': refresh, 'expires_at': expiry, 'refreshing': False})
        auth['bearer_token'] = access
        with closing(vault.connection(path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            vault.plugin_at(conn, plugin['id'], revision)
            current = vault.row_for(conn, plugin['id'])
            if not current or current[2] != claimed: raise SettingsError('mcp_revision_conflict', 409)
            # Rotation keeps the grant revision: existing approvals remain bound
            # to the same issuer/resource/scopes. Logout/replacement increments it.
            save_auth(conn, path, plugin, auth, row[1]); conn.commit()
    except Exception:
        # Persisted refreshing marker deliberately blocks retry after a crash,
        # ambiguous transport failure, or rejected refresh-token rotation.
        raise SettingsError('mcp_oauth_reauthorization_required') from None
