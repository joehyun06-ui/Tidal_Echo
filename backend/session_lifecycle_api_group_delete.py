"""GM-02L default-off whole API version-group logical erasure candidate."""
from dataclasses import dataclass
import re

from . import api_web_generation
from .session_lifecycle import LifecycleError,_session_id
from .session_lifecycle_api_delete import CoordinatedApiDeleteCommands,SCHEMAS,SCOPES,UNSUPPORTED,CLOSED_DDL,REDACTED_META,state_sha
from .session_lifecycle_api_fork import private_json
from .session_lifecycle_coordination import _ReadonlyLegacy
from .session_lifecycle_version_selection import CoordinatedVersionSelectionCommands,VERSION_COLUMNS
from .web_session_delete import _referenced_message_ids,_delete_ids,_redact_ids,_chunks,_quote_identifier

READ='session.api.group-delete.read.v1'
DELETE='session.api.group-delete.v1'


def fail(category='coordination_group_invalid'):raise LifecycleError(category)


@dataclass(frozen=True,repr=False)
class ApiGroupDeleteSnapshot:
    expected_generation:int
    expected_snapshot_sha:str
    expected_graph_sha:str
    expected_group_ids:list[str]
    expected_state_sha:str
    expected_message_ids:list[int]
    expected_redacted_ids:list[int]
    expected_retained_sha:str


class CoordinatedApiGroupDeleteCommands(CoordinatedApiDeleteCommands):
    def _group_config(self,legacy,ids,next_active,expected_sha=None):
        config,rows,active,sha=self._projection(legacy)
        if expected_sha is not None and sha!=expected_sha:fail('coordination_cas_conflict')
        live={r['id']:r for r in rows};group=set(ids)
        if any(sid not in live or live[sid]['provider']!='api' for sid in group):fail('coordination_group_unavailable')
        remaining=[r for r in rows if r['id'] not in group]
        if not any(r['provider']=='api' for r in remaining):fail('coordination_delete_last_api_unsupported')
        if next_active is not None and (next_active in group or not any(r['id']==next_active and r['provider']=='api' for r in remaining)):
            fail('coordination_group_active_invalid')
        if active in group and next_active is None:fail('coordination_delete_active_required')
        return config,sha

    def _group_refs(self,conn,ids):
        refs={'edges':{},'selections':{}};marks=','.join('?' for _ in ids)
        specs=[('web_message_versions',VERSION_COLUMNS,('version_id','root_id','parent_id'),'version_id','edges'),
               ('web_version_selections',{'root_id':('TEXT',1),'version_id':('TEXT',0)},('root_id','version_id'),'root_id','selections')]
        for table,columns,keys,identity,out in specs:
            if not self._table(conn,table,columns):continue
            for column in keys:
                rows=[dict(r) for r in conn.execute('SELECT * FROM '+table+' WHERE '+column+' IN ('+marks+') LIMIT 513',ids)]
                if len(rows)>512:fail('coordination_group_too_large')
                for row in rows:
                    if any(not _session_id(row[k]) for k in keys):fail('coordination_group_graph_invalid')
                    refs[out][row[identity]]=row
        return {name:[rows[k] for k in sorted(rows)] for name,rows in refs.items()}

    @staticmethod
    def _aux_fk(conn):
        # Unknown incoming metadata FKs could cascade or NULL unrelated records.
        # Message FKs are supported separately by retaining redacted message IDs.
        protected=set(SCOPES)|{'web_message_versions','web_version_selections'}
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            for fk in conn.execute('PRAGMA foreign_key_list('+_quote_identifier(row[0])+')'):
                if str(fk['table']).casefold() in protected:fail('coordination_group_aux_fk_unsupported')

    def _group_plan(self,conn,ids,refs,*,closed=False):
        self._database(conn);self._aux_fk(conn)
        present={table:self._table(conn,table,columns) for table,columns in SCHEMAS.items()}
        if not present['messages']:fail('coordination_group_schema_invalid')
        marks=','.join('?' for _ in ids)
        for table,columns in UNSUPPORTED.items():
            if table in {'web_message_versions','web_version_selections'}:continue  # closure checked by caller
            row=conn.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
            if row is None:continue
            actual={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            if row[0]!='table' or not set(columns)<=actual:fail('coordination_group_schema_invalid')
            for column in columns:
                if conn.execute('SELECT 1 FROM '+table+' WHERE '+column+' IN ('+marks+') LIMIT 1',ids).fetchone():
                    fail('coordination_group_reference_unsupported')
        if conn.execute("SELECT 1 FROM messages WHERE CASE WHEN json_valid(meta) THEN json_type(meta)!='object' "
            "OR COALESCE(json_type(meta,'$.api_session') NOT IN ('text','null'),0) ELSE 1 END LIMIT 1").fetchone():
            fail('coordination_delete_state_invalid')
        markers=[r[0] for r in conn.execute('SELECT session_id FROM api_web_closed_sessions WHERE session_id IN ('+marks+') ORDER BY session_id',ids)] if present['api_web_closed_sessions'] else []
        if markers!=(ids if closed else []):fail('coordination_group_closed_conflict')
        rows=[dict(r) for r in conn.execute("SELECT * FROM messages WHERE json_extract(meta,'$.api_session') IN ("+marks+') ORDER BY id LIMIT 5001',ids)]
        if len(rows)>5000:fail('coordination_group_too_large')
        for row in rows:
            try:meta=private_json(row['meta'])
            except LifecycleError:fail('coordination_delete_state_invalid')
            if meta.get('attachments') not in (None,[]):fail('coordination_delete_attachments_unsupported')
        aux={}
        for table,column in SCOPES.items():
            aux[table]=[dict(r) for r in conn.execute('SELECT * FROM '+table+' WHERE '+column+' IN ('+marks+') ORDER BY rowid LIMIT 5001',ids)] if present[table] else []
            if len(aux[table])>5000:fail('coordination_group_too_large')
        if any(r['status'] not in api_web_generation.TERMINAL or r['upstream_unknown']!=0 for r in aux['api_web_generations']):
            fail('coordination_delete_job_unsettled')
        messages=[r['id'] for r in rows];redacted=sorted(_referenced_message_ids(conn,messages));redacted_set=set(redacted)
        retained=[{**r,'kind':'deleted','text':'','meta':REDACTED_META} for r in rows if r['id'] in redacted_set]
        sha=state_sha({'group':ids,'rows':rows,'auxiliary':aux,'present':present,'refs':refs,'referenced':redacted})
        return sha,messages,redacted,state_sha(retained),aux

    def _group_state(self,legacy,conn,root,next_active,access,action):
        metadata,graph,scopes,_=CoordinatedVersionSelectionCommands._inspect(self,legacy,conn,root,root,access,action)
        ids=[r['id'] for r in scopes];self._group_config(legacy,ids,next_active,metadata)
        refs=self._group_refs(conn,ids);group=set(ids)
        if ({r['version_id'] for r in refs['edges']}!=group-{root}
                or any(r['root_id']!=root or r['parent_id'] not in group or r['version_id'] not in group for r in refs['edges'])
                or any(r['root_id']!=root or r['version_id'] not in group for r in refs['selections'])):
            fail('coordination_group_cross_reference')
        if next_active is not None and self._table(conn,'web_message_versions',VERSION_COLUMNS) and conn.execute(
                'SELECT 1 FROM web_message_versions WHERE version_id=?',(next_active,)).fetchone():
            fail('coordination_group_active_invalid')
        state,messages,redacted,retained,aux=self._group_plan(conn,ids,refs)
        return metadata,graph,ids,state,messages,redacted,retained,aux

    def snapshot_group_delete(self,root,*,deleted_at,next_active=None,access):
        self._admit(access,READ);scopes=self._packet(root,next_active,deleted_at)
        guard=lambda:self._guard(access,READ,scopes);guard()
        def observe(legacy,generation,conn):
            metadata,graph,ids,state,messages,redacted,retained,_=self._group_state(legacy,conn,root,next_active,access,READ)
            scopes[:]=[{'id':x,'provider':'api'} for x in sorted(set(ids)|({next_active} if next_active else set()))]
            return ApiGroupDeleteSnapshot(generation,metadata,graph,ids,state,messages,redacted,retained)
        with self.session_lock:return self.coordinator.observe_canonical(observe,guard=guard)

    def delete_api_group(self,root,*,request_id,access,deleted_at,next_active=None,expected_generation,
                         expected_snapshot_sha,expected_graph_sha,expected_group_ids,expected_state_sha,
                         expected_message_ids,expected_redacted_ids,expected_retained_sha,recover=False):
        self._admit(access,DELETE);self._packet(root,next_active,deleted_at)
        if (type(expected_generation) is not int or not 0<=expected_generation<2**63-1 or type(recover) is not bool
                or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v)
                    for v in (expected_snapshot_sha,expected_graph_sha,expected_state_sha,expected_retained_sha))):fail()
        if (type(expected_group_ids) is not list or not 1<=len(expected_group_ids)<=512
                or any(not _session_id(x) for x in expected_group_ids) or expected_group_ids!=sorted(set(expected_group_ids))
                or root not in expected_group_ids or next_active in expected_group_ids):fail()
        for ids in (expected_message_ids,expected_redacted_ids):
            if type(ids) is not list or len(ids)>5000 or any(type(i) is not int or i<=0 for i in ids) or ids!=sorted(set(ids)):fail()
        if not set(expected_redacted_ids)<=set(expected_message_ids):fail()
        group=list(expected_group_ids);messages=list(expected_message_ids);redacted=list(expected_redacted_ids)
        scopes=[{'id':x,'provider':'api'} for x in sorted(set(group)|({next_active} if next_active else set()))]
        guard=lambda:self._guard(access,DELETE,scopes);guard()
        parameters={'contract':'guiting.api-group-delete.v1','action':DELETE,'principal':access.principal_id,
            'root':root,'deleted_at':deleted_at,'next_active':next_active,'expected_generation':expected_generation,
            'expected_snapshot_sha':expected_snapshot_sha,'expected_graph_sha':expected_graph_sha,'expected_group_ids':group,
            'expected_state_sha':expected_state_sha,'expected_message_ids':messages,'expected_redacted_ids':redacted,
            'expected_retained_sha':expected_retained_sha,'scope':'canonical_plain_api_group_only'}
        base=[]
        def mutate(legacy):
            config,_=self._group_config(legacy,group,next_active,expected_snapshot_sha);base[:]=[config]
            next_config=legacy.load_config();next_config['sessions']=[r for r in next_config['sessions'] if r['id'] not in set(group)]
            if next_config.get('deleted_sessions') is None:next_config['deleted_sessions']=[]
            next_config['deleted_sessions'].extend({'id':sid,'provider':'api','deleted_at':deleted_at} for sid in group)
            if next_active is not None:next_config['active_session']=next_active
            legacy.save_config(next_config)
        def effect(conn):
            metadata,graph,ids,state,mids,refs,retained,aux=self._group_state(_ReadonlyLegacy(base[0]),conn,root,next_active,access,DELETE)
            if (metadata,graph,ids,state,mids,refs,retained)!=(expected_snapshot_sha,expected_graph_sha,group,expected_state_sha,messages,redacted,expected_retained_sha):
                fail('coordination_group_state_changed')
            if not self._table(conn,'api_web_closed_sessions',SCHEMAS['api_web_closed_sessions']):conn.execute(CLOSED_DDL)
            for sid in group:conn.execute('INSERT INTO api_web_closed_sessions VALUES (?)',(sid,))
            marks=','.join('?' for _ in group)
            for table,column in SCOPES.items():
                if aux[table]:conn.execute('DELETE FROM '+table+' WHERE '+column+' IN ('+marks+')',group)
            if self._table(conn,'web_message_versions',VERSION_COLUMNS):conn.execute('DELETE FROM web_message_versions WHERE version_id IN ('+marks+')',group)
            if self._table(conn,'web_version_selections',{'root_id':('TEXT',1),'version_id':('TEXT',0)}):
                conn.execute('DELETE FROM web_version_selections WHERE root_id=?',(root,))
            _delete_ids(conn,[i for i in messages if i not in set(redacted)]);_redact_ids(conn,redacted)
        def pending(conn):
            refs=self._group_refs(conn,group)
            if any(refs.values()):fail('coordination_group_effect_changed')
            _,mids,_,_,aux=self._group_plan(conn,group,refs,closed=True)
            if mids or any(aux.values()):fail('coordination_group_effect_changed')
            retained=[]
            for chunk in _chunks(messages):
                retained.extend(dict(r) for r in conn.execute('SELECT * FROM messages WHERE id IN ('+','.join('?' for _ in chunk)+') ORDER BY id',chunk))
            retained.sort(key=lambda r:r['id'])
            if [r['id'] for r in retained]!=redacted or state_sha(retained)!=expected_retained_sha:fail('coordination_group_effect_changed')
        with self.session_lock:
            options={'guard':guard,'expected_generation':expected_generation,'pending_validator':pending}
            if recover:return self.coordinator.recover(request_id,expected_parameters=parameters,**options)
            return self.coordinator.mutate(request_id,parameters,mutate,canonical_effect=effect,**options)
