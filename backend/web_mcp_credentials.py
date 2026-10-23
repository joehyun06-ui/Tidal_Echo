"""Plugin-scoped, write-only HTTP credentials. No process-environment access."""
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from . import web_mcp_settings as settings

MAX_HEADERS = 8
RESERVED_HEADERS = {'authorization', 'host', 'cookie', 'cookie2', 'set-cookie', 'connection',
                    'keep-alive', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'expect',
                    'origin', 'referer', 'user-agent', 'forwarded', 'via', 'range',
                    'last-event-id', 'cache-control', 'pragma', 'date', 'http2-settings',
                    'x-original-url', 'x-rewrite-url', 'x-original-host', 'x-real-ip', 'cf-connecting-ip'}


def header_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,79}', value):
        raise settings.SettingsError('mcp_header_invalid')
    name = value.lower()
    if name in RESERVED_HEADERS or name.startswith(('accept', 'content-', 'mcp-', 'sec-', 'proxy-', 'x-forwarded-', 'x-http-method')):
        raise settings.SettingsError('mcp_header_reserved')
    return name


def validate_headers(value, *, patch=False):
    if not isinstance(value, dict) or len(value) > MAX_HEADERS:
        raise settings.SettingsError('mcp_headers_invalid')
    result = {}
    for key, secret in value.items():
        name = header_name(key)
        if name in result:
            raise settings.SettingsError('mcp_headers_invalid')
        if secret is None and patch:
            result[name] = None
            continue
        if not isinstance(secret, str) or not 1 <= len(secret) <= 8192 or secret.strip() != secret or any(not 32 <= ord(c) <= 126 for c in secret):
            raise settings.SettingsError('mcp_header_value_invalid')
        result[name] = secret
    if sum(len(k) + len(v or '') for k, v in result.items()) > 16384:
        raise settings.SettingsError('mcp_headers_invalid')
    return result


def reference_names(plugin):
    # References describe required names only. Never read the named environment.
    return set(validate_headers({k: 'reference' for k in plugin['options'].get('env_http_headers', {})}))


def secrets(auth):
    return sorted(set(filter(None, [auth.get('bearer_token'), *auth.get('http_headers', {}).values()])), key=len, reverse=True)


def contains_secret(value, auth):
    if isinstance(value, str):
        return any(secret in value for secret in secrets(auth))
    if isinstance(value, dict):
        return any(contains_secret(k, auth) or contains_secret(v, auth) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_secret(v, auth) for v in value)
    return False


def redact(value, auth):
    if isinstance(value, str):
        for secret in secrets(auth):
            value = value.replace(secret, '[redacted]')
    elif isinstance(value, dict):
        value = {redact(k, auth): redact(v, auth) for k, v in value.items()}
    elif isinstance(value, list):
        value = [redact(v, auth) for v in value]
    return value


def scope(plugin):
    value = {k: plugin.get(k) for k in ('id', 'serverId', 'transport', 'url')}
    value['auth'] = {k: plugin['options'].get(k) for k in ('bearer_token_env_var', 'env_http_headers')}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def connection(path):
    conn = settings.connection(path)
    conn.execute('CREATE TABLE IF NOT EXISTS web_mcp_credentials (plugin_id TEXT PRIMARY KEY, scope TEXT NOT NULL, revision INTEGER NOT NULL, ciphertext BLOB, updated_at TEXT NOT NULL)')
    conn.commit()
    return conn


def plugin_at(conn, plugin_id, revision=None):
    inventory = settings.result(conn)
    if revision is not None and revision != inventory['revision']:
        raise settings.SettingsError('mcp_revision_conflict', 409)
    plugin = next((p for p in inventory['plugins'] if p['id'] == plugin_id), None)
    if plugin is None:
        raise settings.SettingsError('mcp_plugin_not_found', 404)
    return plugin, inventory['revision']


def cipher(path, *, create=False):
    target = Path(str(path) + '.mcp-key')
    if create and not target.exists():
        # Publish a complete 0600 file atomically; concurrent writers share one key.
        fd, temporary = tempfile.mkstemp(prefix='.mcp-key-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(Fernet.generate_key()); file.flush(); os.fsync(file.fileno())
            try: os.link(temporary, target)
            except FileExistsError: pass
        finally:
            os.unlink(temporary)
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError()
            return Fernet(file.read(128))
    except (OSError, ValueError):
        raise settings.SettingsError('mcp_credentials_unavailable', 503) from None


def row_for(conn, plugin_id):
    return conn.execute('SELECT scope,revision,ciphertext,updated_at FROM web_mcp_credentials WHERE plugin_id=?', (plugin_id,)).fetchone()


def decode_full(path, plugin, row):
    if not row or not row[2] or row[0] != scope(plugin):
        return {'bearer_token': None, 'http_headers': {}}
    try:
        value = json.loads(cipher(path).decrypt(row[2]))
        if value.get('scope') != scope(plugin) or value.get('plugin_id') != plugin['id']:
            raise ValueError()
        token = value.get('bearer_token')
        auth = {'bearer_token': validate_token(token) if token is not None else None,
                'http_headers': validate_headers(value.get('http_headers', {}))}
        if 'oauth' in value:
            if not isinstance(value['oauth'], dict): raise ValueError()
            auth['oauth'] = value['oauth']
        return auth
    except (InvalidToken, ValueError, TypeError, KeyError):
        raise settings.SettingsError('mcp_credentials_unavailable', 503) from None


def decode(path, plugin, row):
    auth = decode_full(path, plugin, row)
    return {k: auth[k] for k in ('bearer_token', 'http_headers')}


def encode(path, conn, plugin, auth):
    if not (auth.get('bearer_token') or auth.get('http_headers') or auth.get('oauth')):
        return None
    has_ciphertext = conn.execute('SELECT 1 FROM web_mcp_credentials WHERE ciphertext IS NOT NULL LIMIT 1').fetchone()
    # OAuth flows may already depend on the same key, even before credentials exist.
    return cipher(path, create=not has_ciphertext).encrypt(json.dumps({'scope': scope(plugin), 'plugin_id': plugin['id'], **auth}).encode())


def view(conn, path, plugin_id):
    plugin, revision = plugin_at(conn, plugin_id)
    row = row_for(conn, plugin_id)
    auth = decode_full(path, plugin, row)
    oauth = auth.get('oauth')
    configured = bool(auth['bearer_token'] or auth['http_headers'])
    return {'ok': True, 'plugin_id': plugin_id, 'revision': revision,
            'credential_revision': row[1] if row else 0, 'configured': configured,
            'bearer_configured': bool(auth['bearer_token']), 'header_names': sorted(auth['http_headers']),
            'updated_at': row[3] if row else None, 'execution_enabled': False,
            'oauth': {'connected': True, 'issuer': oauth['issuer'], 'scopes': oauth['scopes'],
                      'expires_at': oauth.get('expires_at'), 'reauthorization_required': bool(oauth.get('refreshing') or oauth.get('failed'))}
                     if oauth else None}


def read(path, plugin_id):
    with closing(connection(path)) as conn:
        return view(conn, path, plugin_id)


def validate_token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(not 33 <= ord(c) <= 126 for c in value):
        raise settings.SettingsError('mcp_bearer_invalid')
    return value


def write(path, plugin_id, body, *, remove=False):
    required = {'revision', 'credential_revision'}
    allowed = required | (set() if remove else {'bearer_token', 'http_headers'})
    if not isinstance(body, dict) or not required <= set(body) or set(body) - allowed or (not remove and not set(body) - required) or any(type(body[k]) is not int or not 0 <= body[k] < 2**53-1 for k in required):
        raise settings.SettingsError('mcp_config_invalid')
    if 'bearer_token' in body and body['bearer_token'] is not None:
        validate_token(body['bearer_token'])
    headers = validate_headers(body['http_headers'], patch=True) if 'http_headers' in body else None
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        plugin, _ = plugin_at(conn, plugin_id, body['revision'])
        if not remove and plugin['transport'] != 'streamable-http':
            raise settings.SettingsError('mcp_bearer_not_supported')
        row = row_for(conn, plugin_id)
        if body['credential_revision'] != (row[1] if row else 0):
            raise settings.SettingsError('mcp_revision_conflict', 409)
        auth = {'bearer_token': None, 'http_headers': {}} if remove else decode_full(path, plugin, row)
        if 'bearer_token' in body:
            auth['bearer_token'] = body['bearer_token']
            auth.pop('oauth', None)
        for name, secret in (headers or {}).items():
            if secret is None:
                auth['http_headers'].pop(name, None)
            else:
                auth['http_headers'][name] = secret
        validate_headers(auth['http_headers'])
        ciphertext = encode(path, conn, plugin, auth)
        conn.execute('INSERT INTO web_mcp_credentials VALUES (?,?,?,?,?) ON CONFLICT(plugin_id) DO UPDATE SET scope=excluded.scope,revision=excluded.revision,ciphertext=excluded.ciphertext,updated_at=excluded.updated_at',
                     (plugin_id, scope(plugin), body['credential_revision']+1, ciphertext, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return view(conn, path, plugin_id)


def invalidate(conn, plugins):
    """Called inside the inventory write transaction, including delete/re-add."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_mcp_credentials'").fetchone():
        return
    scopes = {p['id']: scope(p) for p in plugins}
    for plugin_id, old_scope in conn.execute('SELECT plugin_id,scope FROM web_mcp_credentials').fetchall():
        if scopes.get(plugin_id) != old_scope:
            conn.execute('UPDATE web_mcp_credentials SET scope=?,revision=revision+1,ciphertext=NULL,updated_at=? WHERE plugin_id=?',
                         (scopes.get(plugin_id, ''), datetime.now(timezone.utc).isoformat(), plugin_id))


def load_for_inspection(path, plugin, revision):
    """Compatibility helper for callers that only inspect the bearer binding."""
    auth, version = load_for_request(path, plugin, revision)
    return auth['bearer_token'], version


def load_for_request(path, plugin, revision):
    from .web_mcp_oauth import refresh_if_needed
    refresh_if_needed(path, plugin, revision)
    with closing(connection(path)) as conn:
        plugin_at(conn, plugin['id'], revision)
        row = row_for(conn, plugin['id'])
        return decode(path, plugin, row), row[1] if row else 0
