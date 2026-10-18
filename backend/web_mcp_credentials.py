"""Plugin-scoped, write-only bearer credentials. No process-environment access."""
import hashlib
import json
import os
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from . import web_mcp_settings as settings


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


def decode(path, plugin, row):
    if not row or not row[2] or row[0] != scope(plugin):
        return None
    try:
        value = json.loads(cipher(path).decrypt(row[2]))
        if value.get('scope') != scope(plugin) or value.get('plugin_id') != plugin['id']:
            raise ValueError()
        return validate_token(value['bearer_token'])
    except (InvalidToken, ValueError, TypeError, KeyError):
        raise settings.SettingsError('mcp_credentials_unavailable', 503) from None


def view(conn, path, plugin_id):
    plugin, revision = plugin_at(conn, plugin_id)
    row = row_for(conn, plugin_id)
    configured = bool(decode(path, plugin, row))
    return {'ok': True, 'plugin_id': plugin_id, 'revision': revision,
            'credential_revision': row[1] if row else 0, 'configured': configured,
            'updated_at': row[3] if row else None, 'execution_enabled': False}


def read(path, plugin_id):
    with closing(connection(path)) as conn:
        return view(conn, path, plugin_id)


def validate_token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(not 33 <= ord(c) <= 126 for c in value):
        raise settings.SettingsError('mcp_bearer_invalid')
    return value


def write(path, plugin_id, body, *, remove=False):
    fields = {'revision', 'credential_revision'} | (set() if remove else {'bearer_token'})
    if not isinstance(body, dict) or set(body) != fields or any(type(body[k]) is not int or not 0 <= body[k] < 2**53-1 for k in ('revision', 'credential_revision')):
        raise settings.SettingsError('mcp_config_invalid')
    token = None if remove else validate_token(body['bearer_token'])
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        plugin, _ = plugin_at(conn, plugin_id, body['revision'])
        if not remove and (plugin['transport'] != 'streamable-http' or plugin['options'].get('env_http_headers')):
            raise settings.SettingsError('mcp_bearer_not_supported')
        row = row_for(conn, plugin_id)
        if body['credential_revision'] != (row[1] if row else 0):
            raise settings.SettingsError('mcp_revision_conflict', 409)
        ciphertext = None
        if token:
            # If a key is lost, never silently replace it while ciphertext exists.
            has_ciphertext = conn.execute('SELECT 1 FROM web_mcp_credentials WHERE ciphertext IS NOT NULL LIMIT 1').fetchone()
            fernet = cipher(path, create=not has_ciphertext)
            ciphertext = fernet.encrypt(json.dumps({'scope': scope(plugin), 'plugin_id': plugin_id, 'bearer_token': token}).encode())
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
    with closing(connection(path)) as conn:
        plugin_at(conn, plugin['id'], revision)
        row = row_for(conn, plugin['id'])
        return decode(path, plugin, row), row[1] if row else 0
