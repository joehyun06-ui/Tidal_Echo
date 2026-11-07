# GM-02Z: opt-in HTTP mutation-method interlock

After explicitly installing the candidate history routes and before application
startup, a trusted server may call install_api_loop_interlock(loop, enabled=True).
The default is False: no dependency access, middleware, route replacement or
production startup invocation. This is a deliberately restricted HTTP mode, not
a transparent compatibility layer for existing clients.

| Incoming surface in restricted mode | Behavior |
|---|---|
| Exact canonical POST history prepare/execute/status | Existing candidate route and its current auth/ACL/parser |
| Other methods except GET/HEAD/OPTIONS | Check current internal header auth, then 423 before body/legacy handler |
| Missing/wrong/duplicate internal token on blocked writes | 401; auth failure/exception is sanitized |
| GET/HEAD/OPTIONS | Pass through original handlers and their existing auth; no read fence claimed |
| WebSocket | Close 1008; not a mutation bypass transport in this mode |
| Lifespan/other ASGI lifecycle | Forward normally |

The allowlist is fixed server code, not request/body configuration. Both decoded
and raw canonical path must match, avoiding encoded aliases and redirect-based
admission. Unknown nonread methods and unsafe legacy ingress are rejected, not
rerouted or retried. Errors use private/no-store/no-referrer and fixed bodies.
No body read, ID allocation, message/model call or packet/canonical write occurs
for rejected requests. Candidate methods still execute only their existing fixed
operations and independent current permissions.

Installation requires all three candidate POST routes, no previous interlock,
no built middleware stack and no previously registered user middleware. It is
the first registered middleware; later trusted wrappers run outside it, so path
rewrites must occur before this check. Arbitrary trusted middleware or server
code can subvert any application policy; this is not a sandbox for that code.
Proxy/root_path/path-rewrite deployments still require independent verification.

ASGI tests prove rejected requests do not consume body or run legacy handlers,
strict auth/method/path behavior, candidate operations, safe-method passthrough,
installation guards and WebSocket closure. Real loopback TCP tests import the
complete API-loop app, run Uvicorn/lifespan, reject existing config/create/patch/
chat/ingest/login-start writes and compare config/messages/intent/generation;
candidate history prepare/execute/status remains usable. A separate default-off
case demonstrates the legacy config write still behaves as before when this
restricted mode is not installed. Network stays loopback-only in the test child.

This does not fence GET side effects, direct Python calls, background tasks,
manual/external writers, CLI/native traffic, already-running work, other service
processes or all runtime ingress. It neither cancels work nor adds migration,
compensation, production owner/secret provisioning or client recovery UI. Do not
enable it as normal chat production mode: it intentionally blocks legacy chat/
ingest/control HTTP writes until their coordinated paths exist. No production
code startup/flags/dependencies or CI workflow is changed. Frozen baselines,
G3 source truth/derived cards, commercial C independence and production HOLD stay.
