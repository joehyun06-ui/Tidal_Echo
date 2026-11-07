# GM-02W: opt-in internal history-setting HTTP candidate

`install_api_loop(loop, adapter, principal_id=..., enabled=False)` explicitly
assembles an existing PrivateHistorySetting on a FastAPI API-loop surface. Off
means no route registration or dependency access. No startup invocation, env
switch, bootstrap or production installation is added. The server supplies the
stable owner ID; each request calls the current existing check_internal_auth.
Only one nonempty x-api-loop-internal-token header is accepted; query/cookie/body
values cannot supply this credential or principal. The lower-level installer
requires a trusted synchronous authenticator returning exact ConfigCommandAccess.

| POST endpoint | Exact JSON fields | Result |
|---|---|---|
| /loop/lifecycle/history/prepare | request_id, history_n | original request_id/packet_sha, prepared |
| /loop/lifecycle/history/execute | request_id, packet_sha, recover bool | generation, historical replay flag |

Each body is bounded to 4096 streamed bytes, duplicate JSON keys refused, only
identity content encoding supported. No arbitrary dispatch/path/action/auth
fields. Authentication precedes body parsing and private storage access; command
ACL and packet ACL are independently rechecked by existing adapters. Success and
handled error responses are private/no-store/no-referrer with fixed error bodies;
no config, route credential, filesystem path or raw exception is returned.

Clients must durably keep the original returned reference before executing.
Prepare does not execute. Prepared-only recover refuses missing canonical intent.
Execution failure returns outcome_unconfirmed where appropriate, not a success
or safe-to-repeat promise. Connection loss/cancellation may leave the worker
running or the operation committed: use original reference with recover=true,
never allocate a replacement ID automatically. No timeout cancellation, retry,
automatic recovery, client queue or persistence UI is added by this installer.

Real FastAPI/ASGI tests use the exact existing check_internal_auth function AST
from api_loop.py, avoiding full app boot/model/runtime side effects. The guard is
not rewritten; tests do not prove full application startup or live credential
management. Actual request parsing/routing and SQLite/JSON/packets are exercised
through HTTPX ASGITransport without network sockets. This is not a TCP/TLS/proxy
or deployed production HTTP test. A server assembly wrapper is tested against
that guard with server-configured owner identity.

No existing /loop/config or legacy writer is replaced, so all-writer/read/ingress/
runtime fences remain open. The candidate does not certify cookie authentication,
external ACL atomicity, persistent production owner mapping, native/model window
consumption or production backup/recovery. Shared header secret remains a server
credential, not per-device identity. Production flags/defaults remain unchanged.
history_n keeps explicit last-writer semantics without old-draft CAS and does not
delete stored messages. Prior private-FS/fsync/256KiB/orphan-stage, named-path not
inode/antirollback and SQL/JSON limits remain. Production HOLD; G3 truth/derived
ProjectionCard/commercial C independence and frozen baselines unchanged.
