"""Test-only full API-loop/Uvicorn process; loopback only, no production boot."""
import asyncio
import json
import os
from pathlib import Path
import socket
import signal
from contextlib import nullcontext
from unittest.mock import patch

TOKEN='synthetic-internal-'+'x'*48


def serve(path,directory,mode):
    if mode not in ('normal','disabled','fail_publish','interlocked','strict','direct_bound','direct_bound_fail_publish','aliases_bound','runtime_locked','activation_status','activation_status_blocked'):raise ValueError('invalid_test_mode')
    path=Path(path)
    os.environ.update({'API_LOOP_INTERNAL_TOKEN':TOKEN,'RELAY_SECRET':'synthetic-relay-'+'x'*48,
        'LOOP_CONFIG':str(path.with_suffix('.json')),'RELAY_DB':str(path),
        'LLM_API_BASE':'https://synthetic.invalid/v1','LLM_API_KEY':'synthetic-key','LLM_MODEL':'synthetic',
        'RELAY_URL':'http://synthetic.invalid','CODEX_CONTROL_ENABLED':'false','RENDER_TELEGRAM_MVP':'false'})
    actual_connect=socket.socket.connect;actual_dns=socket.getaddrinfo
    def connect(sock,address):
        if not isinstance(address,tuple) or address[0]!='127.0.0.1':raise AssertionError('external_connect_blocked')
        return actual_connect(sock,address)
    def dns(host,*args,**kwargs):
        if host!='127.0.0.1':raise AssertionError('external_dns_blocked')
        return actual_dns(host,*args,**kwargs)
    with patch.object(socket.socket,'connect',connect),patch('socket.getaddrinfo',dns):
        # Prove rejection before a real external connect can be attempted.
        with socket.socket() as probe:
            try:probe.connect(('198.51.100.1',443))
            except AssertionError:pass
            else:raise AssertionError('egress_guard_missing')
        import examples.api_loop as loop
        import uvicorn
        from backend.session_lifecycle_history_http import install_api_loop
        from backend.tests.test_session_lifecycle_history_packet import binding
        adapter=binding(path,Path(directory))
        install_api_loop(loop,adapter,principal_id='synthetic_owner',enabled=mode!='disabled')
        aliases={name:getattr(loop,name) for name in ('load_config','history_n','save_config','update_config','patch_session','create_session','save_sessions')}
        if mode in ('direct_bound','direct_bound_fail_publish','aliases_bound','runtime_locked','activation_status','activation_status_blocked'):
            from backend.session_lifecycle_loop_config_io import install_api_loop_config_io
            from backend.session_lifecycle_commands import ConfigCommandAccess
            install_api_loop_config_io(loop,adapter,access=ConfigCommandAccess('synthetic_owner'),enabled=True)
        if mode in ('runtime_locked','activation_status','activation_status_blocked'):
            from backend.session_lifecycle_runtime_interlock import install_api_loop_runtime_interlock
            install_api_loop_runtime_interlock(loop,enabled=True)
        status_fixture=None
        if mode in ('activation_status','activation_status_blocked'):
            from backend.tests.test_session_lifecycle_storage import FileHarness
            from backend.tests.test_session_lifecycle import CTX
            from backend.session_window_attempts import WindowAttemptJournal
            from backend.session_window_activation_http import install_api_loop as install_status
            status_fixture=FileHarness(path,seed=False)
            status_fixture.repo._coordinator=adapter.commands.coordinator
            install_status(loop,WindowAttemptJournal(status_fixture.repo,enabled=True),access=CTX,enabled=True)
        aliases_guarded=False
        if mode=='aliases_bound':
            from fastapi import HTTPException
            old=aliases['load_config']()
            if aliases['history_n']()!=old['history_n']:raise AssertionError('alias_read_failed')
            for fn,args in ((aliases['save_config'],(old,)),(aliases['update_config'],({'history_n':3},)),
                (aliases['patch_session'],('synthetic',{'pinned':True})),(aliases['create_session'],()),(aliases['save_sessions'],([],))):
                try:fn(*args)
                except HTTPException as e:
                    if e.status_code!=423:raise
                else:raise AssertionError('captured_write_alias_bypassed')
            aliases_guarded=True
        if mode in ('interlocked','strict','activation_status','activation_status_blocked'):
            from backend.session_lifecycle_http_interlock import install_api_loop_interlock
            install_api_loop_interlock(loop,enabled=True,
                strict_reads=mode in ('strict','activation_status','activation_status_blocked'),
                allow_activation_status=mode=='activation_status')
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',0));listener.listen(16)
        config=uvicorn.Config(loop.app,host='127.0.0.1',port=0,loop='asyncio',http='h11',
            lifespan='on',proxy_headers=False,access_log=False,log_level='critical',timeout_graceful_shutdown=2)
        server=uvicorn.Server(config)
        # Test worker handles shutdown explicitly so Uvicorn's signal re-raise
        # does not hide successful lifespan shutdown behind exit code -SIGTERM.
        server.capture_signals=nullcontext
        async def run():
            runtime_guarded=False
            if mode in ('runtime_locked','activation_status','activation_status_blocked'):
                from fastapi import HTTPException
                for call in (lambda:loop.run_model([]),lambda:loop.complete_chat({},[]),lambda:loop.relay_out({})):
                    try:await call()
                    except HTTPException as e:
                        if e.status_code!=423:raise
                    else:raise AssertionError('runtime_entry_bypassed')
                try:loop._provider_client()
                except HTTPException as e:
                    if e.status_code!=423:raise
                else:raise AssertionError('provider_client_bypassed')
                runtime_guarded=True
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,lambda:setattr(server,'should_exit',True))
            task=asyncio.create_task(server.serve(sockets=[listener]))
            for _ in range(500):
                if server.started:break
                if task.done():await task;raise RuntimeError('test_server_not_started')
                await asyncio.sleep(.01)
            else:raise RuntimeError('test_server_start_timeout')
            print(json.dumps({'port':listener.getsockname()[1],'application':'examples.api_loop',
                'lifespan':'started','external_connect_blocked':True,'captured_aliases_guarded':aliases_guarded,
                'runtime_entrypoints_guarded':runtime_guarded,
                'activation_status_allowed':mode=='activation_status'}),flush=True)
            await task
            print(json.dumps({'lifespan':'stopped'}),flush=True)
        try:
            if mode in ('fail_publish','direct_bound_fail_publish'):
                with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('synthetic interrupted publication')):
                    asyncio.run(run())
            else:asyncio.run(run())
        finally:
            listener.close()
            if status_fixture is not None:status_fixture.conn.close()


if __name__=='__main__':
    import sys
    serve(*sys.argv[1:])
