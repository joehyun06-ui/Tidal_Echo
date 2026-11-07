# GM-02AL: explicit historical-status admission in the full app

The HTTP interlock adds allow_activation_status=False. Explicit true requires
the status installer marker and registered POST route before middleware startup.
Only the exact canonical raw POST status path is added to this instance's
allowlist. Existing history metadata paths retain their previous behavior;
strict old reads and legacy writes, chat/native ingress and websocket admission
remain refused. Encoded aliases/trailing slash/other methods are not added.
Installation and admission are separate explicit opt-ins, never startup/env.

Unit ASGI tests verify default refusal, explicit admission, authentication,
marker/precondition checks and canonical raw paths. Full API-loop/Uvicorn child
process tests import examples.api_loop, run its real lifespan with original
internal auth and isolated canonical SQLite/private JSON, install config IO and
runtime interlocks, then query historical activation through strict middleware.
They verify restart status, unchanged data/config/coordinator generation and
continued refusal of real ingest/config/health routes. Another mode installs the
status route but leaves admission off and proves refusal. Child network is
restricted to loopback before full app import; model/native control is disabled.
Lifecycle/runtime/G3 confirmation records are synthetic test fixtures only.

This certifies this offline server composition, not production proxy/TLS or all
writers. Trusted later middleware may rewrite paths and must be reviewed; direct
calls/external workers/old bytecode remain outside HTTP admission. Historical
confirmation is not current runtime/source permission or provider consumption.
No live model/API/Codex adapter, recovery UI, production backup/recovery or public
gateway acceptance. Default off, production HOLD; frozen baselines unchanged.
