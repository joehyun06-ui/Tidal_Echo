# GM-02AK: internal HTTP historical activation status

Explicit default-off installer adds POST /loop/lifecycle/activation/status with
exact conversation_id, activation_request_id, attempt_request_id, packet_sha256
JSON fields. It invokes only the existing query-only status observer in a worker
thread. It never dispatches, activates, creates intents or grants retries.

Authentication precedes body reads/private IO. One internal-token header and a
synchronous trusted AccessScope mapper are required. The API-loop wrapper reuses
the original check_internal_auth and server-owned scope, with no startup call.
Request principal/scope, cookie/query grants, recover/dispatch and extra fields
are rejected. JSON is bounded to 4096 bytes, duplicate keys rejected, compressed
or ambiguous encoding refused. Success and controlled errors carry private,
no-store and no-referrer; errors omit paths/source bodies. Historic confirmation
does not assert current window availability or current G3 source permission.

Tests use real ASGI requests plus real Uvicorn loopback TCP on an isolated app,
original API-loop header auth and disposable canonical SQLite/private JSON.
They do NOT import/start the complete API-loop application or certify its full
middleware/lifespan integration. Existing strict HTTP interlock does not allow
this new path; no admission whitelist is changed. Proxy/TLS/public gateway,
frontend recovery UI, production auth/source authority and API/Codex real
runtime/receipt acceptance remain open. No live model/API calls. No startup/env/
schema/CI/dependency change; default off, production HOLD.
