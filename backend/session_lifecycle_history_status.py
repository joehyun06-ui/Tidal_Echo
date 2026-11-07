"""Default-off original-reference observation; never recover or publish."""
import re
from .session_lifecycle import LifecycleError
from .session_lifecycle_coordination import digest,encode,private_read
from .session_lifecycle_history_packet import PrivateHistorySetting,ACTION,CONTRACT
from .session_lifecycle_request_packets import PacketReference


def fail(category='coordination_status_unavailable'):raise LifecycleError(category)


def inspect_history(adapter, reference, *, access, enabled=False):
    if enabled is not True:fail('lifecycle_disabled')
    if type(adapter) is not PrivateHistorySetting or type(reference) is not PacketReference:
        fail('coordination_invalid')
    adapter._admit(None,access)
    with adapter.session_lock:
        adapter._admit(None,access)
        p=adapter.packets.load(reference.request_id,ACTION,None,expected_sha=reference.packet_sha,access=access)
        if (set(p)!={'contract','session_id','request_id','body'} or p['contract']!=CONTRACT
                or p['session_id'] is not None or p['request_id']!=reference.request_id):fail('coordination_invalid')
        body=adapter._body(p['body']);w=adapter.commands;c=w.coordinator
        if type(w.render_mvp) is not bool or not callable(w._routes):fail()
        routes=w._routes()
        if type(routes) is not list:fail()
        # Same server-owned policy/fallback identity as the original command.
        # No route values or current config are returned to the caller.
        parameters={'contract':'guiting.config.command.v1','principal':access.principal_id,
            'action':ACTION,'target':None,'body':body,'render_mvp':w.render_mvp,'fallback_sha':digest(encode(routes))}
        expected=digest(encode(parameters))
        with c._locked(write=False):
            with c._db(write=False) as conn:
                generation,config_sha=c._meta(conn)
                row=conn.execute('SELECT input_sha,base_generation,old_sha,new_sha,stage,status FROM session_coordination_ops WHERE request_id=?',
                    (reference.request_id,)).fetchone()
                if row is None:
                    c._ready(conn,private_read(c.config))
                    result={'state':'prepared','generation':None,'historical':False}
                else:
                    input_sha,base,old,new,stage,status=row
                    if input_sha!=expected:fail('coordination_request_conflict')
                    if (type(base) is not int or not 0<=base<2**63-1
                            or not all(type(x) is str and re.fullmatch('[0-9a-f]{64}',x) for x in (old,new))
                            or type(stage) is not str or not re.fullmatch(r'config-[A-Za-z0-9_-]+\.stage',stage)):
                        fail()
                    if status=='committed':
                        c._ready(conn,private_read(c.config))
                        if generation<base+1:fail()
                        result={'state':'committed','generation':base+1,'historical':True}
                    elif status=='pending':
                        if (generation,config_sha)!=(base,old):fail()
                        current=digest(private_read(c.config))
                        if current not in (old,new):fail('coordination_external_change')
                        if current==old and old!=new:
                            if digest(private_read(c.directory/stage))!=new:fail()
                        result={'state':'pending','generation':None,'historical':False}
                    else:fail()
                # Do not clean stages, update journals, run callbacks or publish.
                adapter._admit(None,access)
                adapter.packets.load(reference.request_id,ACTION,None,expected_sha=reference.packet_sha,access=access)
                return result
