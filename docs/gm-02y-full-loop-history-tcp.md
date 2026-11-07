# GM-02Y: full API-loop application, lifespan and loopback TCP validation

The test-only child imports the complete existing examples.api_loop module,
explicitly installs the default-off candidate history routes through the existing
install_api_loop wrapper, starts Uvicorn with application lifespan enabled and
binds only 127.0.0.1 on an ephemeral prebound listener. This exercises the actual
application token guard and startup/shutdown, not an extracted function or an
ASGITransport-only application. Every child uses synthetic config/DB/credentials;
model/native control is disabled, socket connections and DNS outside loopback
are rejected before app import. A guarded external-address probe verifies denial.

Six tests cover absent routes when opt-in is off, missing/wrong header auth before
effects, TCP prepare/execute followed by real process restart and original status/
recovery, deliberate discarded success response, pending publication failure
across real server restart, and refusal of client auth/dispatch injection. Each
server startup reports readiness only after Uvicorn startup, and each controlled
shutdown must complete app lifespan and exit cleanly. Test-only signal handling
avoids mistaking Uvicorn's post-shutdown signal re-raise for failed app cleanup.

For the discarded response case a raw TCP client sends execute, the test oracle
waits for durable SQL generation, then closes without reading the response. It
proves recovery of a server-committed request whose response was discarded, not
every possible mid-request TCP failure, provider delivery ambiguity or model
reply delivery. Only status is read after reconnect; recovery uses the original
reference and does not allocate another request ID.

The publication failure mode is a command-line-only test worker configuration,
not an HTTP fault-control endpoint. It prevents JSON publication after durable
intent; status remains pending without publication, including after process
restart. Explicit original-reference recovery on the normal restarted app
finishes once. Test fixture subprocesses, temporary SQLite/JSON/private packet
files and bounded loopback HTTP are the entire scope.

No production code, default app startup, flags, dependencies or legacy routes
are changed. The candidate is explicitly attached inside the isolated test
process; automatic production factory/bootstrap remains absent. Existing legacy
writers/routes remain outside the candidate coordinator. This does not certify
TLS/reverse proxy, production secrets/owner mapping, per-device identity,
all-writer/read/ingress/runtime fences, client durable reference UI, native
windows or production backup/recovery. External/uncooperative writers and prior
private-FS/fsync/size/orphan-stage, named-path versus inode/antirollback,
SQL/JSON non-atomicity and external ACL limits remain. G3 truth/derived cards,
commercial C independence and frozen baselines are unchanged. Production HOLD.
