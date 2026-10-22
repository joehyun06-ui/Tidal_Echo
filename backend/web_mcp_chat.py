"""Opt-in chat plugins and durable, one-use action approvals."""
import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from jsonschema import Draft202012Validator
from referencing import Registry
from . import web_mcp_settings as settings
from . import web_mcp_credentials as credentials
from . import web_mcp_inspection as wire

APPROVAL_SECONDS = 600
MAX_CALLS = 8


class McpError(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def eligible(plugin):
    try:
        credentials.reference_names(plugin)
    except settings.SettingsError:
        return False
    return plugin['enabled'] and plugin['transport'] == 'streamable-http'


def selection_root(conn, sid):
    # Regenerated/edited alternatives remain one visible conversation.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_message_versions'").fetchone():
        row = conn.execute('SELECT root_id FROM web_message_versions WHERE version_id=?', (sid,)).fetchone()
        if row:
            return row[0]
    return sid


def schema_ok(schema):
    # Bounded self-contained schemas only in this adapter. Never fetch references
    # or guess x-mcp-header routing; regex validation needs a bounded engine.
    if not isinstance(schema, dict) or schema.get('type') != 'object' or len(packed(schema).encode()) > 16000:
        return False
    stack, count = [(schema, 0)], 0
    while stack:
        value, depth = stack.pop(); count += 1
        if count > 512 or depth > 16:
            return False
        if isinstance(value, dict):
            if set(value) & {'$ref', '$dynamicRef', 'pattern', 'patternProperties', 'x-mcp-header'}:
                return False
            if '$schema' in value and value['$schema'] != 'https://json-schema.org/draft/2020-12/schema':
                return False
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
    try:
        Draft202012Validator.check_schema(schema)
        return True
    except Exception:
        return False


class Store:
    action_table = 'web_mcp_actions'

    def __init__(self, path):
        self.path = path

    def attach(self, conn):
        pass

    @contextmanager
    def db(self):
        conn = credentials.connection(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS web_mcp_chat_settings (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, config_revision INTEGER NOT NULL, plugins TEXT NOT NULL)')
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.action_table} (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                revision INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, payload TEXT NOT NULL, result TEXT)""")
            conn.execute(f'CREATE INDEX IF NOT EXISTS {self.action_table}_message ON {self.action_table}(session_id,message_id)')
            conn.commit()
            self.attach(conn)
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        finally:
            conn.close()

    def selection(self, conn, sid):
        row = conn.execute('SELECT * FROM web_mcp_chat_settings WHERE session_id=?', (selection_root(conn, sid),)).fetchone()
        return dict(row) if row else {'revision': 0, 'config_revision': 0, 'plugins': '[]'}

    def current(self, conn, sid, mid):
        return conn.execute("SELECT * FROM api_web_generations WHERE session_id=? AND message_id=? AND status='running'", (sid, mid)).fetchone()

    def read(self, sid):
        with self.db() as conn:
            inventory = settings.result(conn)
            value = self.selection(conn, sid)
            return {'ok': True, 'contract_version': 1, 'api_session': sid,
                    'revision': value['revision'], 'config_revision': inventory['revision'],
                    'stale': bool(json.loads(value['plugins'])) and value['config_revision'] != inventory['revision'],
                    'plugin_ids': json.loads(value['plugins']),
                    'plugins': [{'id': p['id'], 'name': p['name'], 'url': p['url']} for p in inventory['plugins'] if eligible(p)]}

    def save(self, sid, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'config_revision', 'plugin_ids'} or any(type(body[k]) is not int for k in ('revision', 'config_revision')):
            raise settings.SettingsError('mcp_config_invalid')
        ids = body['plugin_ids']
        if not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise settings.SettingsError('mcp_config_invalid')
        with self.db() as conn:
            inventory = settings.result(conn); old = self.selection(conn, sid)
            if old['revision'] != body['revision'] or inventory['revision'] != body['config_revision']:
                raise settings.SettingsError('mcp_revision_conflict', 409)
            if set(ids) - {p['id'] for p in inventory['plugins'] if eligible(p)}:
                raise settings.SettingsError('mcp_plugin_not_supported')
            conn.execute('INSERT INTO web_mcp_chat_settings VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,config_revision=excluded.config_revision,plugins=excluded.plugins',
                         (selection_root(conn, sid), old['revision'] + 1, inventory['revision'], packed(ids)))
        return self.read(sid)

    def recover(self):
        with self.db() as conn:
            conn.execute(f"UPDATE {self.action_table} SET status=CASE WHEN status='executing' THEN 'unknown' ELSE 'cancelled' END,revision=revision+1 WHERE status IN ('pending','approved','executing')")

    def end(self, sid, mid):
        with self.db() as conn:
            conn.execute(f"UPDATE {self.action_table} SET status=CASE WHEN status='executing' THEN 'unknown' ELSE 'cancelled' END,revision=revision+1 WHERE session_id=? AND message_id=? AND status IN ('pending','approved','executing')", (sid, mid))

    def action_rows(self, conn, sid):
        return conn.execute("""SELECT a.* FROM web_mcp_actions a JOIN api_web_generations g
            ON g.message_id=a.message_id AND g.session_id=a.session_id
            WHERE a.session_id=? ORDER BY a.created_at DESC LIMIT 24""", (sid,)).fetchall()

    def actions(self, sid):
        with self.db() as conn:
            conn.execute(f"UPDATE {self.action_table} SET status='expired',revision=revision+1 WHERE session_id=? AND status='pending' AND expires_at<?", (sid, time.time()))
            rows = self.action_rows(conn, sid)
            result = []
            for row in reversed(rows):
                value = json.loads(row['payload']); plugin, tool = value['plugin'], value['tool']
                result.append({'id': row['id'], 'generation_id': value.get('generation_id', f"api-gen-{row['message_id']}"),
                               'message_id': row['message_id'], 'revision': row['revision'], 'status': row['status'],
                               'plugin_name': plugin['name'], 'url': plugin['url'], 'tool_name': tool['name'],
                               'arguments': value['arguments'], 'created_at': row['created_at'],
                               'expires_at': row['expires_at'], 'result': row['result']})
            return {'ok': True, 'contract_version': 1, 'api_session': sid, 'actions': result}

    def validate_binding(self, conn, sid, mid, value):
        if not self.current(conn, sid, mid):
            raise McpError('generation_ended')
        selection = self.selection(conn, sid)
        if selection['revision'] != value['selection_revision'] or value['plugin']['id'] not in json.loads(selection['plugins']):
            raise McpError('selection_changed')
        plugin, _ = credentials.plugin_at(conn, value['plugin']['id'], value['config_revision'])
        if not eligible(plugin) or plugin != value['plugin']:
            raise McpError('plugin_changed')
        row = credentials.row_for(conn, plugin['id'])
        if (row[1] if row else 0) != value['credential_revision']:
            raise McpError('credentials_changed')

    def propose(self, sid, mid, value):
        with self.db() as conn:
            self.validate_binding(conn, sid, mid, value)
            count = conn.execute(f'SELECT count(*) FROM {self.action_table} WHERE message_id=? AND session_id=?', (mid, sid)).fetchone()[0]
            if count >= MAX_CALLS:
                raise McpError('tool_limit')
            aid, now = uuid.uuid4().hex, time.time()
            conn.execute(f"INSERT INTO {self.action_table} VALUES (?,?,?,0,'pending',?,?,?,NULL)",
                         (aid, sid, mid, now, now + APPROVAL_SECONDS, packed(value)))
            return aid

    def decision(self, sid, aid, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'decision'} or type(body['revision']) is not int or body['decision'] not in ('approve', 'deny'):
            raise settings.SettingsError('mcp_config_invalid')
        with self.db() as conn:
            row = conn.execute(f'SELECT * FROM {self.action_table} WHERE id=? AND session_id=?', (aid, sid)).fetchone()
            if not row:
                raise settings.SettingsError('mcp_action_not_found', 404)
            if row['revision'] != body['revision'] or row['status'] != 'pending' or row['expires_at'] <= time.time():
                raise settings.SettingsError('mcp_action_conflict', 409)
            if body['decision'] == 'approve':
                try:
                    self.validate_binding(conn, sid, row['message_id'], json.loads(row['payload']))
                except (McpError, settings.SettingsError):
                    raise settings.SettingsError('mcp_action_conflict', 409) from None
            conn.execute(f'UPDATE {self.action_table} SET status=?,revision=revision+1 WHERE id=?',
                         ('approved' if body['decision'] == 'approve' else 'denied', aid))
        return self.actions(sid)

    def state(self, aid):
        with self.db() as conn:
            row = conn.execute(f'SELECT * FROM {self.action_table} WHERE id=?', (aid,)).fetchone()
            if not row:
                raise McpError('action_missing')
            return dict(row)

    def mark(self, aid, status, result=None):
        with self.db() as conn:
            conn.execute(f"UPDATE {self.action_table} SET status=?,result=?,revision=revision+1 WHERE id=? AND status IN ('pending','approved','executing')", (status, result, aid))

    def claim(self, aid):
        with self.db() as conn:
            row = conn.execute(f'SELECT * FROM {self.action_table} WHERE id=?', (aid,)).fetchone()
            if not row or row['status'] != 'approved':
                raise McpError('action_not_approved')
            self.validate_binding(conn, row['session_id'], row['message_id'], json.loads(row['payload']))
            conn.execute(f"UPDATE {self.action_table} SET status='executing',revision=revision+1 WHERE id=?", (aid,))


def prepare(path, sid, mid, *, store=None):
    store = store or Store(path); selection = store.read(sid)
    if not selection['plugin_ids']:
        return None
    if selection['stale']:
        raise McpError('selection_stale')
    inventory = settings.read(path)
    catalog = {}
    for pid in selection['plugin_ids']:
        plugin = next((p for p in inventory['plugins'] if p['id'] == pid), None)
        if not plugin or not eligible(plugin):
            raise McpError('plugin_unavailable')
        if not wire.SLOTS.acquire(blocking=False):
            raise McpError('tools_busy')
        try:
            auth, credential_revision = credentials.load_for_request(path, plugin, inventory['revision'])
            discovered = wire.discover(plugin, include_schemas=True, **auth)
        finally:
            wire.SLOTS.release()
        if discovered['truncated']:
            raise McpError('too_many_tools')
        for tool in discovered['tools']:
            options = plugin['options']
            if tool['name'] in options.get('disabled_tools', []) or ('enabled_tools' in options and tool['name'] not in options['enabled_tools']):
                continue
            if not schema_ok(tool['inputSchema']) or credentials.contains_secret(tool, auth):
                continue
            alias = 'mcp_' + hashlib.sha256(packed([pid, tool['name']]).encode()).hexdigest()[:24]
            catalog[alias] = {'plugin': plugin, 'tool': tool, 'config_revision': inventory['revision'],
                              'credential_revision': credential_revision, 'selection_revision': selection['revision']}
            if len(catalog) > 32:
                raise McpError('too_many_tools')
    if not catalog:
        raise McpError('no_supported_tools')
    return Context(store, sid, mid, catalog)


class Context:
    def __init__(self, store, sid, mid, catalog):
        self.store, self.sid, self.mid, self.catalog = store, sid, mid, catalog

    def definitions(self):
        return [{'name': name, 'description': value['plugin']['name'] + ': ' + value['tool']['description'],
                 'parameters': value['tool']['inputSchema'], 'strict': False} for name, value in self.catalog.items()]

    async def invoke(self, name, arguments):
        if name not in self.catalog or not isinstance(arguments, dict) or len(packed(arguments).encode()) > 16000:
            raise McpError('tool_arguments_invalid')
        value = {**self.catalog[name], 'arguments': arguments}
        if not Draft202012Validator(value['tool']['inputSchema'], registry=Registry()).is_valid(arguments):
            raise McpError('tool_arguments_invalid')
        aid = self.store.propose(self.sid, self.mid, value)
        try:
            while True:
                row = self.store.state(aid)
                if row['expires_at'] <= time.time() and row['status'] == 'pending':
                    self.store.mark(aid, 'expired'); row = self.store.state(aid)
                if row['status'] != 'pending':
                    break
                with self.store.db() as conn:
                    self.store.validate_binding(conn, self.sid, self.mid, value)
                await asyncio.sleep(0.3)
            if row['status'] in ('denied', 'expired'):
                return packed({'status': row['status'], 'executed': False})
            if row['status'] != 'approved':
                raise McpError('action_cancelled')
            return await asyncio.to_thread(self.execute, aid, value)
        except BaseException:
            row = self.store.state(aid)
            self.store.mark(aid, 'unknown' if row['status'] == 'executing' else 'cancelled')
            raise

    def execute(self, aid, value):
        if not wire.SLOTS.acquire(blocking=False):
            self.store.mark(aid, 'failed', '工具繁忙，未执行。')
            raise McpError('tools_busy')
        dispatched = False
        try:
            auth, revision = credentials.load_for_request(self.store.path, value['plugin'], value['config_revision'])
            if revision != value['credential_revision']:
                raise McpError('credentials_changed')
            def claim():
                nonlocal dispatched
                self.store.claim(aid)
                dispatched = True
            result = wire.discover(value['plugin'], include_schemas=True, **auth,
                                   call=(value['tool'], value['arguments']), before_call=claim)
            # Display text only. Never fetch resources/media or follow elicitation.
            content = result.get('content', [])
            if not isinstance(content, list):
                raise McpError('invalid_tool_result')
            texts = [credentials.redact(p['text'], auth) for p in content if isinstance(p, dict) and p.get('type') == 'text' and isinstance(p.get('text'), str)]
            if isinstance(result.get('structuredContent'), dict):
                texts.append(packed(credentials.redact(result['structuredContent'], auth)))
            output = '\n'.join(texts)
            output = output[:16000]
            value_out = packed({'status': 'error' if result.get('isError') else 'completed',
                                'content': output, 'truncated': sum(len(t) for t in texts) > 16000,
                                'untrusted_external_content': True})
            self.store.mark(aid, 'failed' if result.get('isError') else 'completed', output)
            if self.store.state(aid)['status'] == 'unknown':
                raise McpError('tool_result_unknown')
            return value_out
        except Exception:
            self.store.mark(aid, 'unknown' if dispatched else 'failed',
                            '调用已发出，结果未确认；不会自动重试。' if dispatched else '配置或工具发生变化，未执行。')
            raise McpError('tool_result_unknown' if dispatched else 'tool_not_executed') from None
        finally:
            wire.SLOTS.release()
