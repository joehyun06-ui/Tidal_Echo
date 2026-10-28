# 归汀 · OIDC Token Endpoint 受控 HTTPS 发送器（离线验收 Draft）

叠加 PR #150。新增真实 Python 标准库 HTTPS 发送器代码，但 **默认关闭，不存在生产路由和真实 Token Endpoint 调用**。CI 使用 mock DNS、socket/HTTPS connection/response，仅验证决策和 I/O 行为；没有进行真实 TLS 握手、公网 DNS 或实际 OAuth 交换。

## 三重显式启用条件
`MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED=true`、`MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED=true`，以及运行时独立网络批准回调严格返回布尔 `True`。缺少任何一项：拒绝调用，甚至不查 DNS。没有默认批准者，也没有生产组合根将它连接到 #150。只允许调用构造时固定配置的一个完整 HTTPS Token URL；请求必须是既有 #150 的 `OfflineTokenRequest`（POST、固定标准表单头、有界正文），拒绝动态目标和请求头。

## DNS 与 TLS
拒绝 IP literal、localhost 和内部 DNS 名称，DNS 结果不允许私有/loopback/link-local/reserved/non-global/IPv4-mapped IPv6；**全部** DNS 结果都必须通过，避免先挑公共 IP 却忽略私有 IP。由 `_PinnedHttpsConnection` 使用通过审查的**数值 IP**拨号，但 TLS 原始域名继续用于 SNI 和证书主机名检查，采用 `ssl.create_default_context` 系统 CA、`CERT_REQUIRED`、TLS 1.2+。无 HTTP 代理或环境变量代理读取、无重定向跟随、无动态 URL 探测，固定连接端口 443。

请求/响应具备套接字超时、总单次请求逻辑截止时间、响应头数量与值限制、正文最多 12288 字节，并拒绝畸形/重复 Content-Length。网络传输器返回带原始 HTTP 状态、响应头、固定 final URL 和字节正文的 `OfflineTokenResponse`，仍由 #150 的**独立协议解析器**拒绝重定向、非 200、错误 MIME、缓存头、额外凭据和无效 JSON，由 #148 负责签名与 nonce 验证。

**局限与上线阻断**：这不是已通过的生产 HTTP 安全审查，也没有验证真实 CA 证书链、TLS 握手、网络设备、远端 DNS、代理/透明代理干扰、限流、全局超时资源预算和完整授权码兑换。缺少可信真实 IdP/客户端注册、真实浏览器 state/callback、密钥管理和用户同意。CI 的 fake socket 不能证明 Internet 互通。必须单独安全审查并明确授权后才能打开双开关；**本 PR 不启用任何一个**，不合并、不部署、不写用户记忆，不调用付费 OpenAI API；v48 36/36、108/108 继续冻结。
