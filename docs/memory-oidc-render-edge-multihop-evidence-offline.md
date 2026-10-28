# 归汀 · Render / Cloudflare 多跳代理来源证据（离线、默认 OFF）

本阶段堆叠 #163（27/27 工作流、52/52 Job，Python 全回归 2007/2007）。用户已确认 Render 工作区 `ouott`、服务 `ouou-home-telegram` 作为只读核查对象。本次复核的服务信息：Git 分支 `feat/render-telegram-deployment`、启动命令 `python scripts/render_start_p3.py`、自动部署触发器 `off`，HTTP 公网 IP 规则为 `0.0.0.0/0`；最近已知 live 部署为 2026-10-23 的 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`。本阶段没有触发新的部署、修改任何服务参数或访问身份凭据。

## 已验证的事实与不能推断的事实

- Render 公共文档描述 Cloudflare、Render 负载均衡器可能处在应用前面。其说明可为源 IP 追踪提供线索，**但不等价于证实这台具体服务在每条请求上形成了哪种 X-Forwarded-For 链**。多跳、单跳、合并头部、重写头部均应在未得到真实边缘配置前视为未知。
- #156 的 `TrustedOidcProxyProvenanceReview` 仅接受严格的单跳头部和两项测试时注入的 server-only 回调。该模块硬编码合成监听地址 `127.0.0.1:8081`、内部 Host 和精确代理 peer IP，并**未集成在**真实 Render `p3_relay_app` 入口。不得用 `lambda: True` 或根据 `scope.client` / `X-Forwarded-For` 生成的回调声称代理来源已认证。
- #157 对候选 supervisor 的 Uvicorn 增加 `--no-proxy-headers`，#161 对真实候选 P3 入口增加防回退检查。这能防止应用框架层的**隐式改写**，不代表 Cloudflare 或 Render 清除了不可信入站头。未合并的候选代码也未出现在 live 部署中。
- 这次只读取 Render 已有服务元数据、最近部署记录、部署事件的有限摘要，以及按照 `/healthz` 过滤的最多 5 条 HTTP 请求日志；该日志查询未返回记录。**日志为空不证明服务不存在请求、没有攻击，或头部已被清洗。** Render 公共文档说明 HTTP request logs 的可见性可能受工作区套餐与访问权限限制。
- 公网入站规则 `0.0.0.0/0` 表示 Render 边缘允许公网 IPv4 来源，不代表客户端可绕过平台负载均衡器、直接连接应用的内部监听端口。若考虑变更入站限制，还必须独立评估 Telegram Webhook 和其它已有公网客户端兼容性与套餐功能。

## 新增的 Windows / Linux 离线验收

新增 `backend.tests._memory_oidc_render_edge_chain_worker`，对真实 #156 审核类及 Uvicorn 真实 ASGI app stack 进行无 socket 测试：

1. 合成多跳 XFF（两跳、三跳、带/不带空格）、重复 XFF、同时存在 `Forwarded` 和 XFF、RFC 7239 多代理条目必须**拒绝**，而不是从列表中选一个地址认作真实用户。
2. 合成 `CF-Connecting-IP`、`CF-Ray`、`Rndr-Id` 不能构成可信对端或浏览器身份凭据；即便与一个测试时伪造的单跳证明同时出现，审核结果也始终 `may_issue_browser_session=False` 和 `may_write_memory=False`。
3. 回调报告无证明、未知 TCP peer、欺骗的 ASGI `scope.client`、错误公开 Host、混合代理协议头和缺失完整转发头族均 fail closed。
4. 真实 Uvicorn stack 在禁用 proxy header middleware 时，原始 HTTP scope / client / 原始字节头列表不被隐式改写；启用 middleware 的正向对照会重写 scheme，由 #156 原始边界核查拒绝。

**离线测试通过不证明生产代理多跳实际发生，更不证明生产代理清洗规则或 TLS/Host 受保护。** 本阶段没有新增或挂载生产 OIDC 路由，不能签发登录会话；未启用真实 IdP、用户同意、记忆写入或付费 API。所有安全 PR 保持 Draft、未合并、未部署；v48 冻结门槛 **36/36、108/108** 不变。

## 仍需生产证据

必须由有权限的 Render/Cloudflare 操作人员独立核对：边缘连接与头部清洗/覆盖策略、实际每个受保护请求的不可篡改来源证明或可信私有边界、TLS 终止和回调固定域名、内部服务可达性，以及它们和新版 P3 入口的兼容性。即使看到了 request ID 或经过过滤的请求日志，它们也不是原始 TCP peer 的密码学证明。不能在确认之前批准 OIDC/记忆写入上线。

来源参考：
- https://render.com/articles/how-render-handles-ddos-attacks
- https://render.com/docs/logging
- https://render.com/docs/inbound-ip-rules
- https://developers.cloudflare.com/fundamentals/reference/http-headers/
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Forwarded
