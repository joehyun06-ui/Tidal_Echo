# GM-02AR: bounded read-only status transport and opt-in gateway

This is a development/offline integration step after AQ, not production
authentication or formal frontend installation. No startup hook, config/env,
workflow, database schema or production web tree is changed. Both new entry
points default off. Production HOLD; GM-02 overall remains incomplete.

## Verified host map

Read-only source inspection pinned the frontend `joehyun06-ui/ouo-home-ui`
main to `7bdcc2763899566f7f2ba7b7456a74dfa8e1b050`. This is source inspection,
not evidence of its deployed SHA or a signed-in production walkthrough.

| Existing source | Relevant integration contract | Gap / required host work |
|---|---|---|
| `api-client.js:create().request` | Fixed configured HTTPS/loopback base, current Bearer header, redirect:error, 401 callback then Error('401') | Capture the connection-owned request function; never use internal loop token in browser |
| `design-preview/live-chat.js:connect/unauthorized` | New relay/client per connection; `guiting:auth-change`; old connection 401 callback checks relay identity | Invalidate/disable recovery before switching; do not rely only on render timing |
| `design-preview/chat-client.js:conversationOf(state.active)` | Logical conversation/root separate from the currently selected physical version | Use root for the reference/path; invalidate when selected version changes |
| `selectedVersion(root)`, `state.versions`, `state.versionSelections` | Existing version relationships and selection state | Host-owned generation must include connection, root, selected version and relevant session/context changes; a title is not a version |
| `design-preview/login-store.js` | Device/window login records contain Bearer token | Do not use token as AP identity; a stable non-credential account/connection ID is still missing |
| `backend/app.py:check_auth` | Legacy shared Bearer secret (and legacy query-token fallback), returns None | This is not a trusted G3 principal/session/epoch provider. Passing it alone to the gateway cannot authorize observation |
| `backend/session_window_activation_http.py` | Fixed internal POST, internal-header guard, server-owned AccessScope, final scope check | Must independently verify that gateway expected_access is exactly the scope configured on this pinned loop instance |

No frontend files are modified. There is no invented production principal, auth
epoch, stable account ID, recovery capability or automatic identity promotion.
The host still needs authenticated server-side canonical conversation ownership,
current grant/login epoch and current relevant context/version. Tests use synthetic
callbacks, not real login credentials or a production auth claim.

## Server gateway

`backend.session_lifecycle_status_proxy.install(...)` explicitly installs only
POST `/app/sessions/{conversation_id}/lifecycle/activation/status`. Off installs
nothing and does not inspect dependencies. It accepts only the original four
reference fields, never a client principal, scopes, epoch, dispatch/recover flag,
upstream URL or method. The path/root ID must match the body.

The host supplies `authenticate(request, conversation_id) -> TrustedStatusContext`
(sync or async), derived from independent authentication and canonical access.
It must re-read the current scope, authorization/login epoch and context/version
on every invocation. A bare shared-secret check returning None is rejected.
Expected browser Origin is exact and explicitly configured (HTTPS; loopback HTTP
for isolated fixtures). Duplicate Authorization, query strings, browser-supplied
internal headers, encodings and unsupported content types are rejected.

The scope is compared to one explicitly pinned expected_access. This is not a
general multi-tenant proxy: shared internal credentials cannot carry arbitrary
browser-selected scopes. Binding to the wrong loop instance cannot be detected
from the historical status response alone; matching host/loop scope and durable
authorization must be verified before any real installation.

Auth runs before body/client IO, again after the bounded body, and after upstream
observation. Any scope, owner, conversation, auth epoch or context-version change
refuses the result. These checks do not establish independent latestness or
atomicity against changes after the final check.

The upstream is a fixed numeric HTTP loopback origin/port and fixed internal
status path. Only the server-owned internal token and JSON content type are
sent; browser Bearer, cookies, headers, Origin and raw URLs are never forwarded.
trust_env=False, follow_redirects=False, a ten-second total async deadline and
4 KiB request/response limits apply. Unsupported encoding, malformed/duplicate
JSON, unexpected fields, unsafe revision integers and success overclaims fail.
Streams/clients are closed on error, oversize, timeout and completion; no retry
or redirect is performed. Internal 401 becomes a gateway error, not a false
browser logout. Public errors are fixed and private/no-store/no-referrer/nosniff;
no upstream response body, token, header, cookie or exception is reflected.

## Browser transport

`createRecoveryStatusTransport({enabled, request, timeoutMs})` adapts a captured,
authenticated host API request function to the AN/AO/AQ observe contract. It has
no base URL, token getter, storage, polling or action endpoint. Default off never
calls request. Enabled calls use only the fixed encoded session status path,
exact copied reference JSON, no-store, redirect:error and AbortSignal. It does
not initiate calls at construction or on loading/binding a reference.

Successful responses require bounded streamed UTF-8 JSON and the existing strict
three-state contract. Exporting `validateRecoveryStatus` adds no new success
meaning. Historical confirmation never implies a current window, completed
reply or retry authorization. Non-success bodies are cancelled without reading.
The host API's stable Error('401') becomes a 401 result; other raw errors are
hidden. A deadline (1..10000 ms, default 10000) settles even if the host ignores
abort; a late response is cancelled and cannot restore old UI. AQ still owns
context invalidation. Do not pass a request function that silently retries,
follows redirects or represents another connection.

## Verification scope

Backend tests use real FastAPI/ASGI requests through this gateway to the existing
internal status handler and real temporary SQLite/JSON journal with its original
internal auth guard. Three historical states preserve DB/config bytes; controlled
auth/epoch changes, malformed/oversized streams, timeouts, no header forwarding
and no retries are checked. No model, startup or production data is used.

The existing Node wrapper now also executes transport tests. Browser tests run
the actual streamed transport/panel over an authenticated loopback HTTP fixture
with synthetic browser credentials and actual IndexedDB loading. That fixture
does not itself implement the Python gateway; server and browser boundaries are
tested separately. This is not an end-to-end production auth/runtime test. The
existing Chromium/WebKit offline workflow is reused without alteration.

Remaining: independent real login/canonical grant integration, stable non-token
account identity, formal frontend/PWA installation and capability gating, actual
runtime/provider consumption, separate API/Codex acceptance, backup/restore and
release/TLS checks. No merge/deploy or production DB/JSON/env/WAL/Journal changes.
G3 is source/event/revision authority; ProjectionCard is derived. Commercial C
is not a daily dependency. Frozen 36/36,108/108,historical OpenAI 7/7 unchanged.
