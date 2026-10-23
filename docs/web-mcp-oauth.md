# MCP OAuth for the production PWA

This adds HTTP public-client Authorization Code + PKCE S256 login to the existing
encrypted plugin credential vault. API and Codex share the resulting Bearer token;
the existing per-call approval and plugin selection rules remain in force.

## Entry points

- Public GET `/app/mcp/oauth/client-metadata.json` serves a CIMD document.
- Authenticated POST `/app/mcp/plugins/{id}/oauth/start`: inventory `revision`,
  `credential_revision`, optional public `client_id` and exact registered `issuer`.
  Returns an authorization URL, state, expiry, issuer and requested scopes.
- Authenticated POST `/app/mcp/oauth/complete`: `state`, `code` or `error`, optional
  `iss`. The PWA submits this only after matching its original session state.
- Authenticated POST `/app/mcp/plugins/{id}/oauth/logout`: both revisions. Removes
  OAuth access/refresh tokens and pending login, preserves custom headers.
- Existing credential GET adds `oauth` status (issuer, granted scopes, expiry and
  reauthorization-required), never token values, verifier or client credentials.

Production identity is intentionally fixed, never derived from Host/forwarded
headers: client metadata URL is
`https://api.ouoalways.com/relay/app/mcp/oauth/client-metadata.json` and callback is
`https://www.ouoalways.com/design-preview/mcp-oauth-callback.html`.
Other installations must change these two constants and the frontend callback hint
together. Start login from the official PWA domain in the same browser tab. A
provider opening a different browser without the original session storage cannot
complete login; restart from that browser. Preview deployments are not OAuth clients.

## Compatibility and limits

Supports protected-resource metadata via a Bearer challenge or path/root well-known
URLs, OAuth authorization-server metadata and both OIDC discovery path forms.
Issuer and resource metadata are validated before use. The first advertised issuer
is chosen deterministically. Supports CIMD or a user-registered **public** client
with `token_endpoint_auth_method=none`; no Client Secret or dynamic registration.
An existing public client ID is reused only for its exact issuer during reauthorization.
Requested scopes come from the challenge, otherwise protected-resource metadata;
reauthorization preserves previously granted scopes for the same issuer.

Access/refresh tokens and PKCE verifier are encrypted at rest with the existing
0600 key. State is random, hashed in SQLite, expires after ten minutes, and is
consumed before exchanging a code. The callback strips its query immediately,
uses no external resources or referrer, and is excluded from service-worker caches.
The authorization code is briefly held only in the originating tab's sessionStorage
until its one completion request; access/refresh tokens never reach frontend storage.

Every server-side HTTP request pins validated public HTTPS DNS addresses, uses TLS
hostname verification, bounded responses/deadlines/concurrency, and refuses redirects,
proxy/environment credentials and private destinations. Token requests include
the exact resource binding. When advertised, callback `iss` is required; a present
`iss` always requires exact equality, including on error responses.

Refresh is performed before an expired/near-expired token is used. A persisted claim
serializes refresh across workers, without holding a database lock during HTTP.
Refresh rotation preserves the grant revision only when scopes are unchanged;
logout, manual replacement and completed login increment it. A failed/uncertain
refresh or restart during rotation requires reauthorization, never automatic replay.
If no expiry is provided, a subsequent server rejection requires manual reauthorization.
Providers need not issue refresh tokens. There is no automatic scope escalation.

Logout disconnects **归汀 locally**. It does not revoke the provider's account grant
or log the user out of that website; the confirmation explains where to revoke it.
Entering a manual Bearer token replaces OAuth; editing custom headers preserves it.
Changing the plugin address/auth binding invalidates tokens as before. Config and
credential revisions are checked before and after exchanges, so an in-flight login
or refresh cannot resurrect a deleted/replaced credential.

Validation uses synthetic OAuth responses, disposable SQLite, authenticated ASGI
route tests, and Chromium/WebKit callback/UI fixtures in CI. No real account login,
model inference, production chat or external tool call is used for testing.

Protocol references:
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
