"""Explicit restricted API-loop runtime mode; no generation/window activation."""
from types import ModuleType
import inspect
from fastapi import HTTPException

NAMES=('relay_out','_provider_client','stream_responses','complete_responses','stream_chat','complete_chat',
       '_stream_provider','_complete_provider','run_model','run_kelivo_provider_contract','handle_ingest')


def install_api_loop_runtime_interlock(loop,*,enabled=False):
    if enabled is not True:return False
    if (type(loop) is not ModuleType or not getattr(loop,'session_lifecycle_config_io_bound',False)
            or loop.app.middleware_stack is not None
            or getattr(loop,'_session_lifecycle_runtime_interlock',None) is not None):
        raise ValueError('lifecycle_runtime_interlock_binding_invalid')
    for name in NAMES:
        fn=getattr(loop,name,None)
        if not inspect.isfunction(fn) or fn.__globals__ is not loop.__dict__ or fn.__module__!=loop.__name__:
            raise ValueError('lifecycle_runtime_interlock_unwrapped_module_required')
    def refuse():raise HTTPException(status_code=423,detail='lifecycle_runtime_unavailable')
    loop._session_lifecycle_runtime_interlock=refuse
    return True
