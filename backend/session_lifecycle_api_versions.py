"""GM-02K explicit API edit/regenerate prefix copy + version publication.

No inference, native operation, message resend, implicit chat or HTTP install.
"""
from dataclasses import dataclass
import json
import re
import uuid

from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_api_fork import FORK_DDL, history_sha, private_json
from .session_lifecycle_coordination import _ReadonlyLegacy, digest, encode
from .session_lifecycle_version_selection import CoordinatedVersionSelectionCommands, VERSION_COLUMNS, SELECTION_DDL
from .web_session_context import validate as validate_context
from .web_session_provider_authority import WebSessionProviderAuthority

READ='session.api.version-create.read.v1'
CREATE='session.api.version-create.v1'
VERSION_DDL=('CREATE TABLE web_message_versions (version_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, '
    'parent_id TEXT NOT NULL, mode TEXT NOT NULL, message_id INTEGER NOT NULL, point_key INTEGER NOT NULL, prompt_key INTEGER NOT NULL)')


def fail(category='coordination_version_invalid'):raise LifecycleError(category)


def metadata(raw):
    try:value=private_json(raw)
    except LifecycleError:fail('coordination_version_history_invalid')
    if type(value) is not dict:fail('coordination_version_history_invalid')
    return value


@dataclass(frozen=True,repr=False)
class ApiVersionSnapshot:
    expected_generation:int
    expected_snapshot_sha:str
    expected_graph_sha:str
    expected_group_ids:list[str]
    expected_source_sha:str
    expected_copy_sha:str


class CoordinatedApiVersionCommands(CoordinatedVersionSelectionCommands):
    def _version_namespace(self,conn,target):
        # None of these scopes is written by this command. Check again before
        # unpublished recovery, without treating our own copied rows as orphans.
        scopes={'api_web_generations':('session_id',),'api_web_closed_sessions':('session_id',),
            'session_lifecycle_records':('conversation_id',),'web_context_observations':('session_id',),
            'web_mcp_actions':('session_id',),'web_mcp_codex_actions':('session_id',),'web_mcp_chat_settings':('session_id',),
            'channel_conversations':('api_session',),'generation_jobs':('api_session',),
            'codex_sessions':('api_session',),'codex_generation_jobs':('api_session',),
            'usage_ledger':('session_id',),'usage_ledger_events':('session_id',),'usage_ledger_attempts':('session_id',),
            'usage_ledger_issues':('session_id',),'usage_ledger_retired':('session_id',),
            'usage_ledger_retired_modes':('session_id',),'usage_ledger_continuations':('source_id','target_id')}
        for table,columns in scopes.items():
            kind=conn.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
            if kind is None:continue
            actual={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            if kind[0]!='table' or not set(columns)<=actual:fail('coordination_version_namespace_invalid')
            if conn.execute('SELECT 1 FROM '+table+' WHERE '+' OR '.join(c+'=?' for c in columns)+' LIMIT 1',
                            (target,)*len(columns)).fetchone():fail('coordination_version_namespace_conflict')

    def _version_request(self,root,sid,body,title,created_at,select):
        if (not _session_id(root) or not _session_id(sid) or type(select) is not bool or type(body) is not dict
                or set(body)!={'request_id','message_id','mode'} or type(body['request_id']) is not str
                or type(body['message_id']) is not int or not 0<body['message_id']<2**63
                or type(body['mode']) is not str or body['mode'] not in {'edit','regenerate'}):fail()
        try:rid=uuid.UUID(body['request_id']).hex
        except (ValueError,AttributeError):raise LifecycleError('coordination_version_invalid') from None
        target='api-fork-'+rid
        if target in {root,sid}:fail()
        row=self._prepare({'id':target,'title':title,'since_id':0,'created_at':created_at,'pinned':False,'provider':'api'})
        return {'request_id':str(uuid.UUID(rid)),'message_id':body['message_id'],'mode':body['mode']},row

    def _version_state(self,legacy,conn,root,sid,body,row,select,access,action):
        config_sha,graph_sha,scopes,_=self._inspect(legacy,conn,root,sid,access,action)
        self._config(legacy,sid,row,config_sha)
        self.coordinator._fresh_session(conn,row['id'])
        self._version_namespace(conn,row['id'])
        _,has_context=self._tables(conn)
        rows=[dict(r) for r in conn.execute("SELECT id,ts,direction,kind,text,meta FROM messages "
            "WHERE json_extract(meta,'$.api_session')=? AND kind IN ('user','voice','reply') ORDER BY id LIMIT 5001",(sid,))]
        if len(rows)>5000:fail('coordination_version_history_too_large')
        chosen=next((r for r in rows if r['id']==body['message_id']),None)
        if chosen is None or chosen['direction']!=('in' if body['mode']=='edit' else 'out'):
            fail('coordination_version_message_role_invalid')
        prompt=next((r for r in reversed(rows) if r['id']<=chosen['id'] and r['direction']=='in'),None)
        if prompt is None:fail('coordination_version_prompt_unavailable')
        parsed={};first={}
        for r in rows:
            if (any(type(r[k]) is not str for k in ('ts','direction','kind','text','meta'))
                    or r['direction'] not in {'in','out'}):fail('coordination_version_history_invalid')
            value=metadata(r['meta'])
            if value.get('api_session')!=sid:fail('coordination_version_history_invalid')
            if 'version_copy' in value and type(value['version_copy']) is not bool:fail('coordination_version_history_invalid')
            if value.get('attachments') is not None and type(value['attachments']) is not list:fail('coordination_version_history_invalid')
            parsed[r['id']]=value
            if not value.get('version_copy'):first.setdefault(r['direction'],r['id'])
        if parsed[prompt['id']].get('attachments'):fail('coordination_version_prompt_attachment_unsupported')
        parent=None
        if sid!=root:parent=dict(conn.execute('SELECT * FROM web_message_versions WHERE version_id=?',(sid,)).fetchone())
        keys={}
        for r in rows:
            value=parsed[r['id']];key=value.get('version_key')
            if key is None:
                if value.get('version_copy'):fail('coordination_version_history_invalid')
                key=r['id']
                if parent and first.get(r['direction'])==r['id']:
                    if r['direction']=='in':key=parent['prompt_key'] if parent['mode']=='regenerate' else parent['point_key']
                    elif parent['mode']=='regenerate':key=parent['point_key']
            if type(key) is not int or not 0<key<2**63:fail('coordination_version_history_invalid')
            keys[r['id']]=key
        edge={'version_id':row['id'],'root_id':root,'parent_id':sid,'mode':body['mode'],
            'message_id':chosen['id'],'point_key':keys[chosen['id']],'prompt_key':keys[prompt['id']]}
        copied=[]
        for r in rows:
            if r['id']>=prompt['id']:break
            old=parsed[r['id']]
            value={'api_session':row['id'],'source':'web_fork','branch_origin':{'session_id':sid,'message_id':r['id']},
                   'version_copy':True,'version_key':keys[r['id']]}
            if isinstance(old.get('attachments'),list):value['attachments']=old['attachments']
            api=old.get('api');usage=old.get('version_usage') or (api.get('usage') if isinstance(api,dict) else None) or old.get('usage')
            if isinstance(usage,dict):value['version_usage']=usage
            copied.append({**{k:r[k] for k in ('ts','direction','kind','text')},'meta':value})
        context=None
        if has_context:
            value=conn.execute('SELECT * FROM web_session_context WHERE root_id=?',(root,)).fetchone()
            context=dict(value) if value else None
        if context:
            try:validate_context(metadata(context['config']))
            except ValueError:raise LifecycleError('coordination_version_context_invalid') from None
            if (type(context['revision']) is not int or context['revision']<0 or type(context['updated_at']) is not str):
                fail('coordination_version_context_invalid')
        selection=None
        if self._table(conn,'web_version_selections',{'root_id':('TEXT',1),'version_id':('TEXT',0)}):
            previous=conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?',(root,)).fetchone()
            selection=previous[0] if previous else None
        effect={'edge':edge,'messages':copied,'context_root':root,'selection':row['id'] if select else selection}
        try:
            source_sha=history_sha({'rows':rows,'root_context':context,'context_table_present':has_context})
            copy_sha=history_sha(effect)
        except LifecycleError as e:
            fail('coordination_version_history_too_large' if e.category.endswith('history_too_large') else 'coordination_version_history_invalid')
        return config_sha,graph_sha,[r['id'] for r in scopes],source_sha,copy_sha,effect

    def snapshot_version(self,root,sid,body,*,title,created_at,select=True,access):
        self._admit(access,READ);body,row=self._version_request(root,sid,body,title,created_at,select)
        scopes=[{'id':x,'provider':'api'} for x in sorted({root,sid,row['id']})]
        guard=lambda:self._guard(access,READ,scopes);guard()
        row=WebSessionProviderAuthority(None,historical_provider=self._history).normalize_row(row)
        def observe(legacy,generation,conn):
            config,graph,group,source,copy,_=self._version_state(legacy,conn,root,sid,body,row,select,access,READ)
            scopes[:]=[{'id':x,'provider':'api'} for x in sorted(set(group)|{row['id']})]
            return ApiVersionSnapshot(generation,config,graph,group,source,copy)
        with self.session_lock:return self.coordinator.observe_canonical(observe,guard=guard)

    def create_api_version(self,root,sid,body,*,title,created_at,select=True,access,
                            expected_generation,expected_snapshot_sha,expected_graph_sha,expected_group_ids,
                            expected_source_sha,expected_copy_sha,recover=False):
        self._admit(access,CREATE);body,row=self._version_request(root,sid,body,title,created_at,select)
        if (type(expected_generation) is not int or not 0<=expected_generation<2**63-1 or type(recover) is not bool
                or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v)
                    for v in (expected_snapshot_sha,expected_graph_sha,expected_source_sha,expected_copy_sha))):fail()
        if (type(expected_group_ids) is not list or not 1<=len(expected_group_ids)<512
                or any(not _session_id(x) for x in expected_group_ids) or expected_group_ids!=sorted(set(expected_group_ids))
                or not {root,sid}<=set(expected_group_ids) or row['id'] in expected_group_ids):fail()
        expected_group_ids=list(expected_group_ids)
        scopes=[{'id':x,'provider':'api'} for x in sorted(set(expected_group_ids)|{row['id']})]
        guard=lambda:self._guard(access,CREATE,scopes);guard()
        row=WebSessionProviderAuthority(None,historical_provider=self._history).normalize_row(row)
        parameters={'contract':'guiting.api-version-create.v1','action':CREATE,'principal':access.principal_id,
            'root':root,'source':sid,'body':body,'row':row,'select':select,'expected_generation':expected_generation,
            'expected_snapshot_sha':expected_snapshot_sha,'expected_graph_sha':expected_graph_sha,
            'expected_group_ids':expected_group_ids,'expected_source_sha':expected_source_sha,'expected_copy_sha':expected_copy_sha}
        fingerprint=digest(encode(parameters));base=[]
        def mutate(legacy):
            config,_=self._config(legacy,sid,row,expected_snapshot_sha)
            base[:]=[config]
            next_config=legacy.load_config();next_config['sessions'].append(dict(row));legacy.save_config(next_config)
        def effect(conn):
            config,graph,group,source,copy,value=self._version_state(_ReadonlyLegacy(base[0]),conn,root,sid,body,row,select,access,CREATE)
            if (config,graph,group,source,copy)!=(expected_snapshot_sha,expected_graph_sha,expected_group_ids,expected_source_sha,expected_copy_sha):
                fail('coordination_version_source_changed')
            if not self._table(conn,'web_message_versions',VERSION_COLUMNS):conn.execute(VERSION_DDL)
            if not self._tables(conn)[0]:conn.execute(FORK_DDL)
            conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)',tuple(value['edge'][k]
                for k in ('version_id','root_id','parent_id','mode','message_id','point_key','prompt_key')))
            conn.execute('INSERT INTO web_session_forks VALUES (?,?)',(row['id'],fingerprint))
            for message in value['messages']:
                conn.execute('INSERT INTO messages(ts,direction,kind,text,meta) VALUES (?,?,?,?,?)',
                    tuple(message[k] for k in ('ts','direction','kind','text'))+(json.dumps(message['meta'],ensure_ascii=False),))
            if select:
                if not self._table(conn,'web_version_selections',{'root_id':('TEXT',1),'version_id':('TEXT',0)}):conn.execute(SELECTION_DDL)
                conn.execute('INSERT INTO web_version_selections VALUES (?,?) ON CONFLICT(root_id) DO UPDATE SET version_id=excluded.version_id',
                    (root,row['id']))
        def pending(conn):
            self._database(conn)
            self._version_namespace(conn,row['id'])
            if not self._table(conn,'web_message_versions',VERSION_COLUMNS) or not self._tables(conn)[0]:
                fail('coordination_version_effect_changed')
            edge=conn.execute('SELECT * FROM web_message_versions WHERE version_id=?',(row['id'],)).fetchone()
            receipt=conn.execute('SELECT fingerprint FROM web_session_forks WHERE target=?',(row['id'],)).fetchone()
            if not edge or not receipt or receipt[0]!=fingerprint:fail('coordination_version_effect_changed')
            messages=[dict(r) for r in conn.execute("SELECT ts,direction,kind,text,meta FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id LIMIT 5001",(row['id'],))]
            if len(messages)>5000:fail('coordination_version_effect_changed')
            for message in messages:message['meta']=metadata(message['meta'])
            previous=None
            if self._table(conn,'web_version_selections',{'root_id':('TEXT',1),'version_id':('TEXT',0)}):
                value=conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?',(root,)).fetchone()
                previous=value[0] if value else None
            if self._tables(conn)[1] and conn.execute('SELECT 1 FROM web_session_context WHERE root_id=?',(row['id'],)).fetchone():
                fail('coordination_version_effect_changed')
            if history_sha({'edge':dict(edge),'messages':messages,'context_root':root,'selection':previous})!=expected_copy_sha:
                fail('coordination_version_effect_changed')
        with self.session_lock:
            options={'guard':guard,'expected_generation':expected_generation,'pending_validator':pending}
            if recover:return self.coordinator.recover(row['id'],expected_parameters=parameters,**options)
            return self.coordinator.mutate(row['id'],parameters,mutate,require_fresh_session=row['id'],canonical_effect=effect,**options)
