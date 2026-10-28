# 归汀 · P3 实际公网 ASGI 认证与转发头伪造隔离（离线）

本阶段堆叠 Draft #161（25/25 工作流、48/48 Job，2007/2007 Python）。用户已授权对 Render `ouott` 的 `ouou-home-telegram` 只读查询；当前部署分支 `feat/render-telegram-deployment`、入口 `python scripts/render_start_p3.py`，截至本次复核自动部署 `off`，当前服务允许公网 IPv4 的 `0.0.0.0/0`。本阶段**不发送真实公网请求，不查询用户数据，不修改 Render**。

## Render 入站事实及限制

- Render 官方文档指出，Web 服务公网入口是平台负载均衡器，它终止 TLS 并以 HTTP 转发至 `0.0.0.0:$PORT`；**应用内部监听端口本身不直接对公网开放**。因此 `0.0.0.0/0` 不是“绕过负载均衡器的直连漏洞”证明，它只表示 Render 公网层没有配置严格 IP 来源限制。
- Render 的服务 IP allowlist（含更严格的 CIDR）是否可用受订阅和服务类型限制，不能把变更此设置当作未经评估即可上线的默认修复；任何修改还须考虑 Telegram webhook 及现有客户端的公网回调兼容性。
- 平台文档不能凭空保证特定服务的转发头清洗。即使 `--no-proxy-headers` 禁用了 Uvicorn 自动改写，**原始 `X-Forwarded-For`、`Forwarded`、`Host` 仍必须按不可信输入对待**。不能从 `scope.client`、`scope.scheme`、`Host`、`X-Forwarded-Host` 推出浏览器身份、HTTPS 来源或 memory writer 权限。
- 线上最近已查验的 `live` SHA 为 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`（2026-10-23）。所有安全 PR 仍 Draft 且未部署；本阶段测试仅证明**当前候选代码的离线 ASGI 鉴权**。

参考： https://render.com/docs/web-services 、 https://render.com/docs/inbound-ip-rules 、 https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Forwarded-For

## 新增的真实 P3 应用内测试

新建 `backend.tests._memory_oidc_p3_public_ingress_spoof_worker`，直接导入真实的 `backend.p3_relay_app.app`，通过 `httpx.ASGITransport` 在进程内发起请求（禁止真实 TCP 连接）。使用独立临时 SQLite 和完全合成的密钥，仅测试现有访问控制：

- 无 Bearer 时，伪造全局/内网/回环 `X-Forwarded-For`、`X-Forwarded-Proto`、`X-Forwarded-Host`、单跳 `Forwarded`、混合与多跳、重复 `Host`/XFF、恶意 Host、`X-Real-IP` 及 `X-User-Id`/`X-Principal-Id`/`X-Auth-Epoch`/`X-Memory-Identity` 均**不得获得 /app/status 的 200**，预期 401。
- 对 P3 的 `/app/provider/capabilities`、`/app/memory/hybrid-active/status`、`hybrid-shadow/status`、`/app/sessions/{id}/memory/evidence`、`/app/memory/session-write/preflight` 执行未授权测试。记忆预检的 URL `?token=`、重复 Authorization 也必须拒绝。
- 使用合成、合法的 `Authorization: Bearer` 对照 `/app/status`，证明认证机制本身没有被测试短路；健康检查 `/healthz` 本应无需身份。
- 确认现有 P3 并未注册真实 `/auth/oidc/` 路由，身份登录审批仍 OFF。

## 不能推断的结果

**通过本轮测试不等于 Host 校验或代理清洗验收。** 当前无凭据的请求即使携带恶意 Host 也返回 401，只能证明**现有 P3 受保护接口没有直接把这些头当成凭据**；它不能证明携带合法 Bearer 的请求在恶意 Host 下会被拒绝，也不能证明任何未来的 OIDC callback、重定向 URL、Cookie 的 `Secure` 属性、CSRF / Origin 或不可信代理来源得到保护。仍需真实 Render 边缘/域名策略与逐请求可信传输证明，禁止把 #156 的模拟 raw-socket callbacks 接入生产或将静态 `render.yaml` 当作线上证据。

本阶段无生产应用代码改动；只追加离线测试、CI 和文档。保持 Draft、未合并、未部署；真实 IdP/OIDC 登录、浏览器身份会话、记忆写入和付费 API 均 OFF。v48 冻结 **36/36、108/108** 不变。
