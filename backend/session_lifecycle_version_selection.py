"""GM-02J default-off API version selection; no copy, finish, dispatch or HTTP."""
from dataclasses import dataclass
import re

from . import api_web_generation
from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_api_fork import CoordinatedApiForkCommands, history_sha

READ='session.api.version-selection.read.v1'
SELECT='session.api.version-selection.v1'
SELECTION_DDL='CREATE TABLE web_version_selections (root_id TEXT PRIMARY KEY, version_id TEXT NOT NULL)'
VERSION_COLUMNS={'version_id':('TEXT',1),'root_id':('TEXT',0),'parent_id':('TEXT',0),'mode':('TEXT',0),
                 'message_id':('INTEGER',0),'point_key':('INTEGER',0),'prompt_key':('INTEGER',0)}
GENERATION_COLUMNS={'message_id':('INTEGER',1),'session_id':('TEXT',0),'status':('TEXT',0),
    'created_at':('REAL',0),'started_at':('REAL',0),'ended_at':('REAL',0),'model':('TEXT',0),
    'error':('TEXT',0),'upstream_unknown':('INTEGER',0),'reply_id':('INTEGER',0),'notified':('INTEGER',0)}


def fail(category='coordination_selection_invalid'):raise LifecycleError(category)


@dataclass(frozen=True,repr=False)
class VersionSelectionSnapshot:
    expected_generation:int
    expected_snapshot_sha:str
    expected_graph_sha:str
    expected_group_ids:list[str]


class CoordinatedVersionSelectionCommands(CoordinatedApiForkCommands):
    version_creation_integrated=False
    runtime_selection_confirmed=False

    @staticmethod
    def _table(conn,name,columns):
        try:return CoordinatedApiForkCommands._table(conn,name,columns)
        except LifecycleError:fail('coordination_selection_schema_invalid')

    def _selection_request(self,root,body):
        if (not _session_id(root) or type(body) is not dict or set(body)!={'version_id'}
                or not _session_id(body.get('version_id'))):fail()
        return {'version_id':body['version_id']}

    def _inspect(self,legacy,conn,root,target,access,action):
        self._database(conn)
        has_versions=self._table(conn,'web_message_versions',VERSION_COLUMNS)
        has_selection=self._table(conn,'web_version_selections',{'root_id':('TEXT',1),'version_id':('TEXT',0)})
        has_jobs=self._table(conn,'api_web_generations',GENERATION_COLUMNS)
        has_closed=self._table(conn,'api_web_closed_sessions',{'session_id':('TEXT',1)})
        if has_versions and conn.execute('SELECT 1 FROM web_message_versions WHERE version_id=?',(root,)).fetchone():
            fail('coordination_selection_root_required')
        edges=[dict(r) for r in conn.execute('SELECT * FROM web_message_versions WHERE root_id=? ORDER BY version_id LIMIT 513',(root,))] if has_versions else []
        if len(edges)>511:fail('coordination_selection_graph_too_large')
        nodes={root}
        for edge in edges:
            if (not _session_id(edge['version_id']) or not _session_id(edge['parent_id'])
                    or edge['version_id']==root or edge['mode'] not in {'edit','regenerate'}
                    or any(type(edge[k]) is not int or edge[k]<=0 for k in ('message_id','point_key','prompt_key'))):
                fail('coordination_selection_graph_invalid')
            nodes.add(edge['version_id'])
        parents={edge['version_id']:edge['parent_id'] for edge in edges}
        for sid in parents:
            current=sid;seen=set()
            while current!=root:
                if current in seen or current not in parents:fail('coordination_selection_graph_invalid')
                seen.add(current);current=parents[current]
        scopes=[{'id':sid,'provider':'api'} for sid in sorted(nodes|{target})]
        # Discover identifiers under root scope, authorize all group IDs before
        # history or config normalization; never read/return message content.
        self._guard(access,action,scopes)
        _,rows,_,metadata=self._projection(legacy)
        live={r['id']:r for r in rows}
        if root not in live or target not in nodes:fail('coordination_selection_target_unavailable')
        if any(sid not in live for sid in nodes):fail('coordination_selection_graph_unavailable')
        if any(live[sid]['provider']!='api' for sid in nodes):fail('coordination_native_selection_unsupported')
        previous=conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?',(root,)).fetchone() if has_selection else None
        selected=previous[0] if previous else root
        if selected not in nodes:fail('coordination_selection_graph_invalid')
        jobs=[];closed=[]
        # IDs are bounded by the existing 512-row config cap. Values remain
        # parameterized; reading one group cannot mutate job expiration/status.
        ordered=sorted(nodes);marks=','.join('?' for _ in ordered)
        tool_tables=[]
        for table in ('web_mcp_actions','web_mcp_codex_actions'):
            kind=conn.execute('SELECT type FROM sqlite_master WHERE name=?',(table,)).fetchone()
            tool_tables.append(kind is not None)
            if kind is None:continue
            columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            if kind[0]!='table' or 'session_id' not in columns:fail('coordination_selection_schema_invalid')
            if conn.execute('SELECT 1 FROM '+table+' WHERE session_id IN ('+marks+') LIMIT 1',ordered).fetchone():
                fail('coordination_selection_tools_unsupported')
        if has_jobs:
            jobs=[dict(r) for r in conn.execute('SELECT * FROM api_web_generations WHERE session_id IN ('+marks+') ORDER BY message_id LIMIT 5001',ordered)]
            if len(jobs)>5000:fail('coordination_selection_graph_too_large')
            if any(j['status'] not in api_web_generation.TERMINAL or j['upstream_unknown']!=0 for j in jobs):
                fail('coordination_selection_job_unsettled')
        if has_closed:
            closed=[r[0] for r in conn.execute('SELECT session_id FROM api_web_closed_sessions WHERE session_id IN ('+marks+') ORDER BY session_id',ordered)]
            if closed:fail('coordination_selection_closed')
        value={'contract':'guiting.version-selection.graph.v1','root':root,'edges':edges,
            'stored_selection':previous[0] if previous else None,'jobs':jobs,'closed':closed,
            'present':[has_versions,has_selection,has_jobs,has_closed],'tool_tables':tool_tables}
        try:sha=history_sha(value)
        except LifecycleError:fail('coordination_selection_graph_invalid')
        return metadata,sha,scopes,has_selection

    def snapshot_selection(self,root,body,*,access):
        self._admit(access,READ);body=self._selection_request(root,body)
        scopes=[{'id':sid,'provider':'api'} for sid in {root,body['version_id']}]
        guard=lambda:self._guard(access,READ,scopes);guard()
        def observe(legacy,generation,conn):
            metadata,graph,discovered,_=self._inspect(legacy,conn,root,body['version_id'],access,READ)
            scopes[:]=discovered
            return VersionSelectionSnapshot(generation,metadata,graph,[r['id'] for r in discovered])
        with self.session_lock:return self.coordinator.observe_canonical(observe,guard=guard)

    def select_version(self,root,body,*,request_id,access,expected_generation,expected_snapshot_sha,
                       expected_graph_sha,expected_group_ids,recover=False):
        self._admit(access,SELECT);body=self._selection_request(root,body)
        if (type(expected_generation) is not int or not 0<=expected_generation<2**63-1 or type(recover) is not bool
                or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v) for v in (expected_snapshot_sha,expected_graph_sha))):fail()
        if (type(expected_group_ids) is not list or not 1<=len(expected_group_ids)<=512
                or any(not _session_id(sid) for sid in expected_group_ids)
                or expected_group_ids!=sorted(set(expected_group_ids))
                or not {root,body['version_id']}<=set(expected_group_ids)):fail()
        expected_group_ids=list(expected_group_ids)
        scopes=[{'id':sid,'provider':'api'} for sid in expected_group_ids]
        guard=lambda:self._guard(access,SELECT,scopes);guard()
        parameters={'contract':'guiting.version-selection.command.v1','action':SELECT,'principal':access.principal_id,
            'root':root,'body':body,'expected_generation':expected_generation,
            'expected_snapshot_sha':expected_snapshot_sha,'expected_graph_sha':expected_graph_sha,
            'expected_group_ids':expected_group_ids}
        def effect(conn,legacy):
            metadata,graph,discovered,has_selection=self._inspect(legacy,conn,root,body['version_id'],access,SELECT)
            scopes[:]=discovered
            if (metadata!=expected_snapshot_sha or graph!=expected_graph_sha
                    or [r['id'] for r in discovered]!=expected_group_ids):fail('coordination_cas_conflict')
            if not has_selection:conn.execute(SELECTION_DDL)
            conn.execute('INSERT INTO web_version_selections VALUES (?,?) ON CONFLICT(root_id) DO UPDATE SET version_id=excluded.version_id',
                         (root,body['version_id']))
        with self.session_lock:
            if recover:return self.coordinator.recover(request_id,expected_parameters=parameters,guard=guard)
            return self.coordinator.mutate_canonical(request_id,parameters,effect,guard=guard,expected_generation=expected_generation)
