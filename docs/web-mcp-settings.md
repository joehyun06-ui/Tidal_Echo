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
# 手动连接检测（2026-10-15）

`POST /app/mcp/plugins/{id}/inspect` 使用 relay 鉴权，body 为 `{ "revision": 1 }`。
返回 `status`、`checked_at`、`tools`（名称/说明）、`protocol_version`、`truncated` 和恒为 false 的 `execution_enabled`。
检测前后核对配置 revision；变更返回 409。结果不持久化，不代表持续在线。

仅公网 HTTPS 443 Streamable HTTP。DNS 全部地址校验后固定连接 IP，保留原域名 TLS 校验；不跟随跳转、不用代理、不读取任意环境变量。带凭据引用的配置返回 `credentials_not_connected`，STDIO/旧 SSE 不启动连接。
支持 2026-07-28 每请求元数据，以及 2025-11-25/06-18/03-26 初始化握手和 session 清理。只发 tools/list、必要的 initialize/initialized 与 session DELETE，绝不发 tools/call。服务器工具描述仅为未受信任的显示文本，不授予权限。

最多 2 个工作线程、15 秒网络预算、512 KiB 每响应、4 页、100 个工具；HTTP 18 秒返回超时。系统 DNS 若延迟，仍占用原工作槽直到返回，不增长并发。客户端断开不会自动重试。

协议来源：https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning 和 https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http 。

