"""Explicit pre-startup binding of selected API-loop config globals only."""
from pathlib import Path
from types import ModuleType
import inspect
from fastapi import HTTPException
from .session_lifecycle import LifecycleError
from .session_lifecycle_commands import ConfigCommandAccess
from .session_lifecycle_history_packet import PrivateHistorySetting
from .session_lifecycle_history_read import read_history

NAMES=('load_config','history_n','save_config','update_config','patch_session','create_session','save_sessions')


def install_api_loop_config_io(loop,adapter,*,access,enabled=False):
    """Install before traffic on an unwrapped module; no startup invocation.
    Not an OS/file capability boundary. Captured old function aliases, native/
    external processes and whole multi-call transactions are not fenced here.
    """
    if enabled is not True:return False
    if (type(loop) is not ModuleType or type(adapter) is not PrivateHistorySetting
            or type(access) is not ConfigCommandAccess
            or getattr(loop,'session_lifecycle_config_io_bound',False)):
        raise ValueError('lifecycle_config_io_binding_invalid')
    adapter._admit(None,access)
    writer=adapter.commands;c=writer.coordinator;bound_lock=adapter.session_lock
    bound_paths=(Path(c.config),Path(c.canonical_path),Path(c.directory))
    try:
        paths=(Path(loop.LOOP_CONFIG),Path(loop.RELAY_DB))
        if (paths!=(Path(c.config),Path(c.canonical_path))
                or any(not p.is_absolute() or p.resolve()!=p for p in paths)
                or loop.app.middleware_stack is not None):
            raise ValueError('lifecycle_config_io_binding_invalid')
    except (AttributeError,TypeError,OSError,RuntimeError):
        raise ValueError('lifecycle_config_io_binding_invalid') from None
    originals={name:getattr(loop,name,None) for name in NAMES}
    if any(not inspect.isfunction(fn) or fn.__globals__ is not loop.__dict__ or fn.__module__!=loop.__name__
           for fn in originals.values()):raise ValueError('lifecycle_config_io_unwrapped_module_required')

    def unavailable(exc):
        status=403 if exc.category in ('coordination_access_denied','coordination_access_unavailable') else 409 if exc.category=='coordination_recovery_required' else 503
        return HTTPException(status_code=status,detail='lifecycle_config_read_unavailable')
    def resource():
        if (adapter.commands is not writer or writer.coordinator is not c
                or adapter.session_lock is not bound_lock or writer.session_lock is not bound_lock
                or (Path(c.config),Path(c.canonical_path),Path(c.directory))!=bound_paths
                or (Path(loop.LOOP_CONFIG),Path(loop.RELAY_DB))!=bound_paths[:2]):
            raise HTTPException(status_code=503,detail='lifecycle_config_binding_changed')
    def guard():
        resource();adapter._admit(None,access)
    def load_config():
        try:
            guard()
            def project(legacy,_):
                config=legacy.load_config()
                if loop.RENDER_TELEGRAM_MVP:
                    loop.deployment_config.validate_loop_config_payload(config,render_mvp=True)
                return config
            with adapter.session_lock:
                return c.observe_config(project,guard=guard)
        except LifecycleError as exc:raise unavailable(exc) from None
        except (OSError,ValueError,TypeError,RuntimeError):
            raise HTTPException(status_code=503,detail='lifecycle_config_read_unavailable') from None
    def history_n():
        try:
            guard()
            return read_history(adapter,access=access,enabled=True)['history_n']
        except LifecycleError as exc:raise unavailable(exc) from None
        except (OSError,ValueError,TypeError,RuntimeError):
            raise HTTPException(status_code=503,detail='lifecycle_config_read_unavailable') from None
    def refused(*args,**kwargs):
        raise HTTPException(status_code=423,detail='lifecycle_direct_write_blocked')
    def checked(original):
        def run(*args,**kwargs):
            if kwargs.get('coordinated') is not writer:
                raise HTTPException(status_code=423,detail='lifecycle_direct_write_blocked')
            resource()
            return original(*args,**kwargs)
        return run
    # Plain ModuleType and all validation precede assignment. Startup assembly
    # must not race running code; no runtime install/uninstall or lease is added.
    replacements={'load_config':load_config,'history_n':history_n,'save_config':refused,
        'update_config':checked(originals['update_config']),'patch_session':checked(originals['patch_session']),
        'create_session':refused,'save_sessions':refused}
    for name,value in replacements.items():setattr(loop,name,value)
    loop.session_lifecycle_config_io_bound=True
    return True
