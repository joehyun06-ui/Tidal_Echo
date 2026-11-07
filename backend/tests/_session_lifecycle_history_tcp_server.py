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
    if mode not in ('normal','disabled','fail_publish','interlocked','strict'):raise ValueError('invalid_test_mode')
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
        if mode in ('interlocked','strict'):
            from backend.session_lifecycle_http_interlock import install_api_loop_interlock
            install_api_loop_interlock(loop,enabled=True,strict_reads=mode=='strict')
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',0));listener.listen(16)
        config=uvicorn.Config(loop.app,host='127.0.0.1',port=0,loop='asyncio',http='h11',
            lifespan='on',proxy_headers=False,access_log=False,log_level='critical',timeout_graceful_shutdown=2)
        server=uvicorn.Server(config)
        # Test worker handles shutdown explicitly so Uvicorn's signal re-raise
        # does not hide successful lifespan shutdown behind exit code -SIGTERM.
        server.capture_signals=nullcontext
        async def run():
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,lambda:setattr(server,'should_exit',True))
            task=asyncio.create_task(server.serve(sockets=[listener]))
            for _ in range(500):
                if server.started:break
                if task.done():await task;raise RuntimeError('test_server_not_started')
                await asyncio.sleep(.01)
            else:raise RuntimeError('test_server_start_timeout')
            print(json.dumps({'port':listener.getsockname()[1],'application':'examples.api_loop',
                'lifespan':'started','external_connect_blocked':True}),flush=True)
            await task
            print(json.dumps({'lifespan':'stopped'}),flush=True)
        try:
            if mode=='fail_publish':
                with patch('backend.session_lifecycle_coordination.atomic_write_text',side_effect=OSError('synthetic interrupted publication')):
                    asyncio.run(run())
            else:asyncio.run(run())
        finally:listener.close()


if __name__=='__main__':
    import sys
    serve(*sys.argv[1:])
