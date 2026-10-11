# MCP configuration inventory

`GET /app/mcp/settings` and `PUT /app/mcp/settings` use existing relay authentication
and return `Cache-Control: private, no-store`. The relay is a single-user service;
devices connected to that service share this inventory in its persistent SQLite DB.

PUT accepts `{revision, plugins}`. Every successful write increments the revision;
stale writes return 409, including stale deletions. GET returns `storage: server`,
`connection_status: not_connected` and `execution_enabled: false` explicitly.
At most 50 unique plugin IDs/server IDs/URLs and 256 KiB per request are accepted.
Fields follow the existing UI subset of Codex MCP configuration:
https://developers.openai.com/codex/mcp

This is an inventory, not a connection manager. No URL is fetched, no command is
executed, no environment variable is read, and neither native Codex config nor
generation permissions are changed. HTTP credentials are represented only by
environment variable names. Literal `env` and `http_headers` objects are rejected.
Legacy SSE entries remain storable but cannot be exported as native Codex TOML.

The PWA uses server storage when authenticated, never falls back to local writes on
server errors, and requires a refresh after an uncertain write or version conflict.
Unauthenticated local drafts remain available. Import is explicit, preserves the
local backup, rejects collisions and uses the same revision check. Logout invalidates
pending responses and clears the displayed inventory. Tool connection detection,
discovery, credentials provisioning and action approval remain separate next steps.

Validation uses synthetic configurations, concurrent SQLite writers, browser
fixtures and the real isolated P3 relay. No production plugins are connected.
