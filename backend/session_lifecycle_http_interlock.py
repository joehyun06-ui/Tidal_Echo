"""Explicit HTTP admission interlock, not an all-writer/runtime fence."""
from fastapi import Request,HTTPException
from fastapi.responses import JSONResponse
from .session_lifecycle_history_http import PREFIX,HEADERS
from .session_window_activation_http import PATH as ACTIVATION_STATUS

ALLOWED=frozenset(PREFIX+'/'+name for name in ('prepare','execute','status'))
READ_METHODS=frozenset({'GET','HEAD','OPTIONS'})


class HistoryHttpInterlock:
    production_supported=False
    all_writers_integrated=False

    def __init__(self,app,*,check_internal_auth,strict_reads=False,allow_activation_status=False):
        self.app,self.check_internal_auth=app,check_internal_auth
        self.strict_reads=strict_reads
        self.allowed=ALLOWED | ({ACTIVATION_STATUS} if allow_activation_status else set())

    async def __call__(self,scope,receive,send):
        if scope['type']=='websocket':
            await send({'type':'websocket.close','code':1008});return
        if scope['type']!='http':
            await self.app(scope,receive,send);return
        method=scope.get('method','')
        path=scope.get('path','');raw=scope.get('raw_path',b'')
        candidate=(method=='POST' and path in self.allowed and raw==path.encode('ascii'))
        read_candidate=(method=='GET' and path==PREFIX and raw==PREFIX.encode('ascii'))
        if candidate or (read_candidate if self.strict_reads else method in READ_METHODS):
            await self.app(scope,receive,send);return
        # Do not read a rejected body or enter any legacy mutation handler.
        request=Request(scope)
        status=423;error='lifecycle_reader_blocked' if method in READ_METHODS else 'lifecycle_writer_blocked'
        try:
            tokens=request.headers.getlist('x-api-loop-internal-token')
            if len(tokens)!=1 or not tokens[0]:raise HTTPException(status_code=401)
            self.check_internal_auth(request)
        except HTTPException as exc:
            status=403 if exc.status_code==403 else 401;error='unauthorized'
        except Exception:
            status=503;error='lifecycle_auth_unavailable'
        await JSONResponse({'ok':False,'error':error},status_code=status,headers=HEADERS)(scope,receive,send)


def install_api_loop_interlock(loop,*,enabled=False,strict_reads=False,allow_activation_status=False):
    """Install before startup, only after candidate routes were explicitly added.
    No startup invocation or env switch is added. The HTTP-only restricted mode
    intentionally refuses chat/native/legacy mutation routes until integrated.
    Reads pass through unless strict_reads=True explicitly restricts them to the
    coordinated GET candidate. Direct calls, background tasks and external
    writers remain outside. Proxy/path rewrite behavior is not certified.
    """
    if enabled is not True:return False
    if type(strict_reads) is not bool or type(allow_activation_status) is not bool:
        raise ValueError('lifecycle_interlock_binding_invalid')
    app=loop.app
    if (not callable(loop.check_internal_auth) or app.middleware_stack is not None
            or getattr(app.state,'lifecycle_http_interlock_installed',False)):
        raise ValueError('lifecycle_interlock_binding_invalid')
    routes={r.path for r in app.routes if 'POST' in (getattr(r,'methods',None) or ())}
    if not ALLOWED.issubset(routes):raise ValueError('lifecycle_interlock_candidate_missing')
    if allow_activation_status and (ACTIVATION_STATUS not in routes
            or getattr(app.state,'lifecycle_activation_status_installed',False) is not True):
        raise ValueError('lifecycle_interlock_activation_status_missing')
    if strict_reads and not any(getattr(r,'path',None)==PREFIX and 'GET' in (getattr(r,'methods',None) or ()) for r in app.routes):
        raise ValueError('lifecycle_interlock_read_candidate_missing')
    # Must be the first registered middleware, hence innermost to future trusted
    # wrappers: any future path-rewriting middleware must run before this check.
    if app.user_middleware:raise ValueError('lifecycle_interlock_middleware_order_invalid')
    app.add_middleware(HistoryHttpInterlock,check_internal_auth=loop.check_internal_auth,
                       strict_reads=strict_reads,allow_activation_status=allow_activation_status)
    app.state.lifecycle_http_interlock_installed=True
    return True
