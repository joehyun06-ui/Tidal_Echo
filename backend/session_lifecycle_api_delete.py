"""GM-02I: opt-in canonical API content erasure and recoverable tombstone.

Only plain API sessions without attachments/external tool/version/lifecycle
references. Historical receipts never claim complete current/global erasure.
"""
from dataclasses import dataclass
import json
import re

from . import api_web_generation
from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_api_fork import CoordinatedApiForkCommands, history_sha, private_json
from .web_session_delete import _referenced_message_ids, _delete_ids, _redact_ids, _chunks

READ='session.api.delete.read.v1'
DELETE='session.api.delete.v1'
CLOSED_DDL='CREATE TABLE api_web_closed_sessions (session_id TEXT PRIMARY KEY)'
REDACTED_META=json.dumps({'deleted':True,'api_session':'__deleted__'},sort_keys=True,separators=(',',':'))
SCHEMAS={
    'messages':{'id':('INTEGER',1),'ts':('TEXT',0),'direction':('TEXT',0),
        'kind':('TEXT',0),'text':('TEXT',0),'meta':('TEXT',0)},
    'api_web_closed_sessions':{'session_id':('TEXT',1)},
    'api_web_generations':{'message_id':('INTEGER',1),'session_id':('TEXT',0),'status':('TEXT',0),
        'created_at':('REAL',0),'started_at':('REAL',0),'ended_at':('REAL',0),'model':('TEXT',0),
        'error':('TEXT',0),'upstream_unknown':('INTEGER',0),'reply_id':('INTEGER',0),'notified':('INTEGER',0)},
    'web_session_context':{'root_id':('TEXT',1),'revision':('INTEGER',0),'config':('TEXT',0),'updated_at':('TEXT',0)},
    'web_context_observations':{'session_id':('TEXT',1),'payload':('TEXT',0)},
    'web_session_forks':{'target':('TEXT',1),'fingerprint':('TEXT',0)},
}
SCOPES={'web_session_context':'root_id','web_context_observations':'session_id',
        'web_session_forks':'target','api_web_generations':'session_id'}
UNSUPPORTED={
    'web_message_versions':('version_id','root_id','parent_id'),
    'web_version_selections':('root_id','version_id'),
    'session_lifecycle_records':('conversation_id',),
    'web_mcp_actions':('session_id',),'web_mcp_codex_actions':('session_id',),
    'web_mcp_chat_settings':('session_id',),'channel_conversations':('api_session',),
    'generation_jobs':('api_session',),'codex_sessions':('api_session',),
    'codex_generation_jobs':('api_session',),'usage_ledger':('session_id',),
    'usage_ledger_events':('session_id',),'usage_ledger_attempts':('session_id',),
    'usage_ledger_issues':('session_id',),'usage_ledger_retired':('session_id',),
    'usage_ledger_retired_modes':('session_id',),'usage_ledger_continuations':('source_id','target_id'),
}


def fail(category='coordination_delete_invalid'):raise LifecycleError(category)


def state_sha(value):
    try:return history_sha(value)
    except LifecycleError as e:
        fail('coordination_delete_state_too_large' if e.category.endswith('history_too_large')
             else 'coordination_delete_state_invalid')


@dataclass(frozen=True,repr=False)
class ApiDeleteSnapshot:
    expected_generation:int
    expected_snapshot_sha:str
    expected_state_sha:str
    expected_message_ids:list[int]
    expected_redacted_ids:list[int]
    expected_retained_sha:str


class CoordinatedApiDeleteCommands(CoordinatedApiForkCommands):
    external_cleanup_supported=False
    g3_delete_supported=False

    @staticmethod
    def _table(conn,name,columns):
        try:return CoordinatedApiForkCommands._table(conn,name,columns)
        except LifecycleError:fail('coordination_delete_schema_invalid')

    def _packet(self,sid,next_active,deleted_at):
        if not _session_id(sid) or sid in {'__legacy__','__deleted__'}:fail()
        if next_active is not None and (not _session_id(next_active) or next_active==sid):fail()
        self._prepare({'id':sid,'title':'delete marker','since_id':0,'created_at':deleted_at,'pinned':False,'provider':'api'})
        return [{'id':value,'provider':'api'} for value in (sid,next_active) if value is not None]

    def _delete_config(self,legacy,sid,next_active,expected_sha=None):
        config,rows,active,sha=self._projection(legacy)
        if expected_sha is not None and sha!=expected_sha:fail('coordination_cas_conflict')
        target=next((r for r in rows if r['id']==sid),None)
        if target is None:fail('coordination_delete_session_unavailable')
        if target['provider']!='api':fail('coordination_native_delete_unsupported')
        remaining=[r for r in rows if r['id']!=sid]
        if not any(r['provider']=='api' for r in remaining):fail('coordination_delete_last_api_unsupported')
        if next_active is not None and not any(r['id']==next_active and r['provider']=='api' for r in remaining):
            fail('coordination_delete_active_invalid')
        if active==sid and next_active is None:fail('coordination_delete_active_required')
        return config,sha

    def _state(self,conn,sid,*,closed=False):
        self._database(conn)
        present={table:self._table(conn,table,columns) for table,columns in SCHEMAS.items()}
        if not present['messages']:fail('coordination_delete_schema_invalid')
        for table,columns in UNSUPPORTED.items():
            row=conn.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
            if row is None:continue
            actual={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            if row[0]!='table' or not set(columns)<=actual:fail('coordination_delete_schema_invalid')
            if conn.execute('SELECT 1 FROM '+table+' WHERE '+' OR '.join(c+'=?' for c in columns)+' LIMIT 1',
                            (sid,)*len(columns)).fetchone():fail('coordination_delete_reference_unsupported')
        if conn.execute("SELECT 1 FROM messages WHERE CASE WHEN json_valid(meta) THEN "
            "json_type(meta)!='object' OR COALESCE(json_type(meta,'$.api_session') NOT IN ('text','null'),0) ELSE 1 END LIMIT 1").fetchone():
            fail('coordination_delete_state_invalid')
        marker=present['api_web_closed_sessions'] and bool(conn.execute(
            'SELECT 1 FROM api_web_closed_sessions WHERE session_id=?',(sid,)).fetchone())
        if marker!=closed:fail('coordination_delete_closed_conflict')
        rows=[dict(r) for r in conn.execute("SELECT * FROM messages WHERE json_extract(meta,'$.api_session')=? ORDER BY id LIMIT 5001",(sid,))]
        if len(rows)>5000:fail('coordination_delete_state_too_large')
        for row in rows:
            try:meta=private_json(row['meta'])
            except LifecycleError:fail('coordination_delete_state_invalid')
            if meta.get('attachments') not in (None,[]):fail('coordination_delete_attachments_unsupported')
        auxiliary={}
        for table,column in SCOPES.items():
            values=[dict(r) for r in conn.execute('SELECT * FROM '+table+' WHERE '+column+'=? ORDER BY rowid LIMIT 5001',(sid,))] if present[table] else []
            if len(values)>5000:fail('coordination_delete_state_too_large')
            auxiliary[table]=values
        for job in auxiliary['api_web_generations']:
            if job['status'] not in api_web_generation.TERMINAL or job['upstream_unknown']!=0:
                fail('coordination_delete_job_unsettled')
        ids=[r['id'] for r in rows]
        referenced=sorted(_referenced_message_ids(conn,ids))
        retained=[{**r,'kind':'deleted','text':'','meta':REDACTED_META} for r in rows if r['id'] in set(referenced)]
        sha=state_sha({'rows':rows,'auxiliary':auxiliary,'present':present,'referenced':referenced})
        return sha,ids,referenced,state_sha(retained),auxiliary

    def snapshot_delete(self,sid,*,next_active=None,deleted_at,access):
        self._admit(access,READ);scopes=self._packet(sid,next_active,deleted_at)
        guard=lambda:self._guard(access,READ,scopes);guard()
        def observe(legacy,generation,conn):
            _,metadata=self._delete_config(legacy,sid,next_active)
            sha,ids,redacted,retained,_=self._state(conn,sid)
            return ApiDeleteSnapshot(generation,metadata,sha,ids,redacted,retained)
        with self.session_lock:return self.coordinator.observe_canonical(observe,guard=guard)

    def delete_conversation(self,sid,*,request_id,access,deleted_at,next_active=None,
                            expected_generation,expected_snapshot_sha,expected_state_sha,
                            expected_message_ids,expected_redacted_ids,expected_retained_sha,recover=False):
        self._admit(access,DELETE);scopes=self._packet(sid,next_active,deleted_at)
        if (type(expected_generation)is not int or not 0<=expected_generation<2**63-1 or type(recover)is not bool
                or any(type(v)is not str or not re.fullmatch('[0-9a-f]{64}',v)
                    for v in (expected_snapshot_sha,expected_state_sha,expected_retained_sha))):fail()
        for ids in (expected_message_ids,expected_redacted_ids):
            if (type(ids)is not list or len(ids)>5000 or any(type(i)is not int or i<=0 for i in ids)
                    or ids!=sorted(set(ids))):fail()
        if not set(expected_redacted_ids)<=set(expected_message_ids):fail()
        # Freeze caller-owned mutable lists before entering callbacks.
        expected_message_ids=list(expected_message_ids);expected_redacted_ids=list(expected_redacted_ids)
        guard=lambda:self._guard(access,DELETE,scopes);guard()
        parameters={'contract':'guiting.api-delete.command.v1','action':DELETE,'principal':access.principal_id,
            'target':sid,'deleted_at':deleted_at,'next_active':next_active,'expected_generation':expected_generation,
            'expected_snapshot_sha':expected_snapshot_sha,'expected_state_sha':expected_state_sha,
            'expected_message_ids':expected_message_ids,'expected_redacted_ids':expected_redacted_ids,
            'expected_retained_sha':expected_retained_sha,'scope':'canonical_plain_api_only'}
        def mutate(legacy):
            config,_=self._delete_config(legacy,sid,next_active,expected_snapshot_sha)
            config['sessions']=[r for r in config['sessions'] if r['id']!=sid]
            if config.get('deleted_sessions') is None:config['deleted_sessions']=[]
            config['deleted_sessions'].append({'id':sid,'provider':'api','deleted_at':deleted_at})
            if next_active is not None:config['active_session']=next_active
            legacy.save_config(config)
        def effect(conn):
            sha,ids,redacted,retained,aux=self._state(conn,sid)
            if (sha,ids,redacted,retained)!=(expected_state_sha,expected_message_ids,expected_redacted_ids,expected_retained_sha):
                fail('coordination_delete_state_changed')
            if not self._table(conn,'api_web_closed_sessions',SCHEMAS['api_web_closed_sessions']):conn.execute(CLOSED_DDL)
            conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(sid,))
            for table,column in SCOPES.items():
                if aux[table]:conn.execute('DELETE FROM '+table+' WHERE '+column+'=?',(sid,))
            _delete_ids(conn,[i for i in ids if i not in set(redacted)])
            _redact_ids(conn,redacted)
        def pending(conn):
            _,ids,_,_,aux=self._state(conn,sid,closed=True)
            if ids or any(aux.values()):fail('coordination_delete_effect_changed')
            retained=[]
            for chunk in _chunks(expected_message_ids):
                retained.extend(dict(r) for r in conn.execute('SELECT * FROM messages WHERE id IN ('+','.join('?' for _ in chunk)+') ORDER BY id',chunk))
            retained.sort(key=lambda r:r['id'])
            if ([r['id'] for r in retained]!=expected_redacted_ids or state_sha(retained)!=expected_retained_sha):
                fail('coordination_delete_effect_changed')
        with self.session_lock:
            options={'guard':guard,'expected_generation':expected_generation,'pending_validator':pending}
            if recover:return self.coordinator.recover(request_id,expected_parameters=parameters,**options)
            return self.coordinator.mutate(request_id,parameters,mutate,canonical_effect=effect,**options)
