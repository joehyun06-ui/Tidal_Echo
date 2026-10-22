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

Saving the inventory does not fetch a URL or execute a command. No environment
variable is read, and neither native Codex config nor
generation permissions are changed. HTTP credentials are represented only by
environment variable names. Literal `env` and `http_headers` objects are rejected.
Legacy SSE entries remain storable but cannot be exported as native Codex TOML.

The PWA uses server storage when authenticated, never falls back to local writes on
server errors, and requires a refresh after an uncertain write or version conflict.
Unauthenticated local drafts remain available. Import is explicit, preserves the
local backup, rejects collisions and uses the same revision check. Logout invalidates
pending responses and clears the displayed inventory. Explicit inspection and
write-only HTTP credentials are described below. Saving credentials does not grant
execution permission; API and supported Codex sessions use separate per-call approvals.

Validation uses synthetic configurations, concurrent SQLite writers, browser
fixtures and the real isolated P3 relay. No production plugins are connected.
# 手动连接检测（2026-10-15）

`POST /app/mcp/plugins/{id}/inspect` 使用 relay 鉴权，body 为 `{ "revision": 1 }`。
返回 `status`、`checked_at`、`tools`（名称/说明）、`protocol_version`、`truncated` 和恒为 false 的 `execution_enabled`。
检测前后核对配置 revision；变更返回 409。结果不持久化，不代表持续在线。

仅公网 HTTPS 443 Streamable HTTP。DNS 全部地址校验后固定连接 IP，保留原域名 TLS 校验；不跟随跳转、不用代理、不读取任意环境变量。未绑定的凭据引用返回 `credentials_not_connected`，STDIO/旧 SSE 不启动连接。
支持 2026-07-28 每请求元数据，以及 2025-11-25/06-18/03-26 初始化握手和 session 清理。只发 tools/list、必要的 initialize/initialized 与 session DELETE，绝不发 tools/call。服务器工具描述仅为未受信任的显示文本，不授予权限。

最多 2 个工作线程、15 秒网络预算、512 KiB 每响应、4 页、100 个工具；HTTP 18 秒返回超时。系统 DNS 若延迟，仍占用原工作槽直到返回，不增长并发。客户端断开不会自动重试。

协议来源：https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning 和 https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http 。

## 插件密钥（2026-10-19）

`GET/PUT/DELETE /app/mcp/plugins/{id}/credentials` 沿用 relay 鉴权及 private/no-store。
GET 仅返回 configured、配置 revision、credential_revision、updated_at，恒有 execution_enabled:false。PUT 接收 `{revision,credential_revision,bearer_token}`，DELETE 接收两个 revision。拒绝未知字段；不回显密钥或异常详情。

Bearer 密钥用 cryptography Fernet 认证加密，密文存 SQLite；随机主密钥在同一持久目录的 `<DB_PATH>.mcp-key`，权限 0600，完整文件原子发布。备份/迁移需同时保留数据库和此文件；文件丢失或权限异常时拒绝解密，不用登录密钥或环境变量代替它。

密文绑定插件 ID、serverId、传输方式、完整 URL 和认证引用。修改这些字段或删除配置时，在同一 SQLite 事务内清空绑定；改回旧地址不会恢复。普通名称更改保留绑定。凭据自身也有 CAS revision；检测前后核对，检测中的旧结果不覆盖新密钥状态。

只对已验证并固定 IP 的原地址发送 Authorization: Bearer；不跟随跳转、不转发归汀登录密钥、不读取配置中指定的环境变量。OAuth 尚未接入。工具响应中直接回显的已知密钥会被遮蔽。密钥不写浏览器存储、普通配置或 TOML。

主密钥与数据库由同一个服务器账户访问；此加密保护数据库副本，不代表服务器本身失陷后仍能保密。保存密钥不授予模型或工具任何执行权限。

## 自定义认证头（2026-10-22）

同一 credentials endpoint 扩展为原子局部更新：PUT 必须携带两个 revision，
并提供 bearer_token 和/或 http_headers。缺省字段保留；bearer_token:null 单独清除
Bearer；http_headers:{"X-Api-Key":"新值"} 添加或替换，{"x-api-key":null} 单独移除。
DELETE 仍清除全部凭据。旧 Bearer 密文无需迁移；第一次局部更新保留其内容。
GET 额外返回 bearer_configured 与小写、排序后的 header_names，绝不返回值。

名称忽略大小写，支持英文字母、数字和短横线，1–80 字符；最多 8 个头。
值为 1–8192 可打印 ASCII 字符，不含首尾空格、换行或控制字符；头的名称和值
合计最多 16 KiB，请求体最多 64 KiB。重复大小写名称拒绝。Authorization 只走
Bearer 栏；Host、Cookie、连接/路由/代理头、MCP 元数据、Accept、Content-*、
Sec-* 等保留字段禁止覆盖，不能通过这些字段改变目标或传输协议。

env_http_headers 仅声明需要的名称：必须在此插件密钥窗口手动绑定相应安全请求头，
不会读取它指向的环境变量。非法或保留名称仍不可执行。没有声明也可直接添加认证头。
API 与 Codex 共用 vault/传输；所有握手、发现、调用、旧协议 session 清理均使用
相同绑定。仍只请求固定的公网 HTTPS 443 地址，不跟随跳转、不转发到其他 URL。
新凭据 revision 使旧待确认动作失效；发出后的操作不会自动重试。

发现工具时，回显密钥的原始执行元数据会被过滤；展示描述在截断前遮蔽直接回显。
工具文本与 structuredContent 在序列化/截断前遮蔽全部已绑定密钥值。
这针对已知值的直接回显，不是对任意编码、变形输出的通用内容识别。
协议依据：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
与 https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization 。


