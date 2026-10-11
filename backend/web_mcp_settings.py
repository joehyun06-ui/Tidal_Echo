"""Persistent MCP configuration inventory. Saving never connects or executes."""
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlsplit


class SettingsError(ValueError):
    def __init__(self, category, status=400):
        self.category, self.status = category, status
        super().__init__(category)


def text(value, limit, *, empty=False):
    if not isinstance(value,str) or len(value)>limit or (not empty and not value.strip()) or any(ord(c)<32 for c in value):
        raise SettingsError('mcp_config_invalid')
    return value


def env(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,119}',value):
        raise SettingsError('mcp_env_name_invalid')
    return value


def validate_plugin(value):
    if not isinstance(value,dict): raise SettingsError('mcp_config_invalid')
    transport=value.get('transport')
    if not isinstance(transport,str) or transport not in {'stdio','sse','streamable-http'}: raise SettingsError('mcp_transport_invalid')
    fields={'id','name','serverId','transport','enabled','options'}|({'command','args'} if transport=='stdio' else {'url'})
    if set(value)!=fields or type(value['enabled']) is not bool: raise SettingsError('mcp_config_invalid')
    for key,pattern in [('id',r'[A-Za-z0-9_-]{1,160}'),('serverId',r'[A-Za-z0-9_-]{1,64}')]:
        if not isinstance(value[key],str) or not re.fullmatch(pattern,value[key]): raise SettingsError('mcp_config_invalid')
    text(value['name'],60)
    if transport=='stdio':
        text(value['command'],300)
        if not isinstance(value['args'],list) or len(value['args'])>100: raise SettingsError('mcp_config_invalid')
        for arg in value['args']: text(arg,1000,empty=True)
    else:
        url=text(value['url'],2048)
        try:
            parsed=urlsplit(url);port=parsed.port
            valid=bool(parsed.hostname) and (parsed.scheme=='https' or parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'})
            if not valid or parsed.username or parsed.password or '?' in url or '#' in url or any(c.isspace() for c in url) or '\\' in url: raise ValueError()
        except ValueError: raise SettingsError('mcp_url_invalid') from None
    options=value['options']
    allowed={'startup_timeout_sec','tool_timeout_sec','required','enabled_tools','disabled_tools'}|({'env_vars','cwd'} if transport=='stdio' else {'bearer_token_env_var','env_http_headers'})
    if not isinstance(options,dict) or set(options)-allowed: raise SettingsError('mcp_options_invalid')
    for key,val in options.items():
        if key.endswith('_timeout_sec'):
            if type(val) not in (int,float) or not math.isfinite(val) or not 0<val<=3600: raise SettingsError('mcp_options_invalid')
        elif key=='required':
            if type(val) is not bool: raise SettingsError('mcp_options_invalid')
        elif key=='bearer_token_env_var': env(val)
        elif key=='cwd': text(val,1000)
        elif key=='env_http_headers':
            if not isinstance(val,dict) or len(val)>30: raise SettingsError('mcp_options_invalid')
            for header,variable in val.items():
                if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',header): raise SettingsError('mcp_options_invalid')
                env(variable)
        else:
            if not isinstance(val,list) or len(val)>100: raise SettingsError('mcp_options_invalid')
            for item in val: env(item) if key=='env_vars' else text(item,160)
    return value


def validate_plugins(values):
    if not isinstance(values,list) or len(values)>50: raise SettingsError('mcp_config_invalid')
    ids=set();servers=set();urls=set()
    for item in values:
        validate_plugin(item)
        if item['id'] in ids or item['serverId'] in servers or item.get('url') and item['url'] in urls: raise SettingsError('mcp_config_duplicate')
        ids.add(item['id']);servers.add(item['serverId'])
        if item.get('url'): urls.add(item['url'])
    return values


def connection(path):
    conn=sqlite3.connect(path,timeout=5)
    conn.execute('CREATE TABLE IF NOT EXISTS web_mcp_settings (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,plugins TEXT NOT NULL,updated_at TEXT NOT NULL)')
    conn.commit()
    return conn


def result(conn):
    row=conn.execute('SELECT revision,plugins,updated_at FROM web_mcp_settings WHERE id=1').fetchone()
    return {'ok':True,'contract_version':1,'revision':row[0] if row else 0,
            'plugins':validate_plugins(json.loads(row[1])) if row else [],'updated_at':row[2] if row else None,
            'storage':'server','connection_status':'not_connected','execution_enabled':False}


def read(path):
    with closing(connection(path)) as conn: return result(conn)


def save(path,body):
    if not isinstance(body,dict) or set(body)!={'revision','plugins'} or type(body['revision']) is not int or not 0<=body['revision']<2**53-1: raise SettingsError('mcp_config_invalid')
    plugins=validate_plugins(body['plugins'])
    serialized=json.dumps(plugins,ensure_ascii=False,allow_nan=False)
    if len(serialized.encode())>262144: raise SettingsError('mcp_config_too_large',413)
    with closing(connection(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current=result(conn)
        if body['revision']!=current['revision']: raise SettingsError('mcp_revision_conflict',409)
        conn.execute('INSERT INTO web_mcp_settings VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,plugins=excluded.plugins,updated_at=excluded.updated_at',
                     (current['revision']+1,serialized,datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return result(conn)
