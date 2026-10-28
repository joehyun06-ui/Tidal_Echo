# 归汀 · Render TLS 卸载与 OIDC HTTPS 方案冲突审查（OFF）

本轮堆叠 #164（28/28 工作流、53/53 CI Job；Python 2007/2007）。该阶段重点不是继续假设或批准代理清洗，而是**证明当前未合并的两个独立安全约束，在 Render 当前公开的入口部署模型中存在功能兼容性问题**。

## 已核实的生产服务配置（2026-10-28，只读）

- 工作区 `ouott`；服务 `ouou-home-telegram`（`srv-d9dk0gbrjlhs73apgufg`）。
- Git 仓库 `joehyun06-ui/Tidal_Echo`；分支 `feat/render-telegram-deployment`。
- 启动命令 `python scripts/render_start_p3.py`；监听端口 `10000/TCP`。
- Render 公开域名 `https://ouou-home-telegram.onrender.com`；`renderSubdomainPolicy=enabled`；默认健康检查 `/healthz`；一台实例，`starter` 套餐，Oregon 地区。
- `autoDeployTrigger=off`；公网入口 `0.0.0.0/0`；最近 live 部署为 2026-10-23 的 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`（源于已核实的只读 Render Deploys）。
- 未从 Render 连接器得到这一具体服务的边缘 XFF 清洗、不可变原始 TCP peer、已验证自定义域名或原始客户端 HTTPS 证明；不能从以上配置推断。
- **本次没有读取任何环境变量的值，没有改动 Render 服务，且没有向线上发出登录、OIDC、记忆或其它用户数据请求。**

## 实际问题

Render 文档明确：Render 在 edge 上终止浏览器的 HTTPS，再由负载均衡器通过**内部明文 HTTP**连接应用。向 Uvicorn 下游转发的正常 ASGI `scope.scheme` 因而通常是 `http`。

安全链同时包含：
1. #157 / #161 的候选 Uvicorn 启动命令显式 `--no-proxy-headers`。这可阻断框架自行信任 X-Forwarded-Proto 并改写 `scope.scheme`；**不能据此证明原始客户端走了 HTTPS**。
2. #153 的 `OidcBrowserCallbackOfflineReview._basic_request` 必须见到 `request.url.scheme == "https"`，且拒绝 `X-Forwarded-Proto` / `Forwarded` 等可欺骗头部。#163 对这套独立审核通过了大量合成 HTTPS ASGI 测试。

因此不能同时断言“在 Render 当前内部 HTTP 卸载拓扑中，不信任代理头”与“OIDC reviewer 正常看到 scheme=https”。如果把该审核直接上线，合法 browser start / callback 也会失败。**这属于确定的设计兼容性阻断，不是证实 Render 已存在安全漏洞；真实 OIDC 路由从未部署。**

## 新增 15 项离线实测

`backend.tests._memory_oidc_render_tls_scheme_worker` 在 Windows 和 Linux 使用真实的 `create_isolated_oidc_asgi_review`、`OidcBrowserCallbackOfflineReview`、`httpx.ASGITransport` 以及真实 `uvicorn.Config.load()` 后的 app stack，无外部 socket：

- 即使浏览器 `Origin: https://app.example.test` 精确匹配，**内部 HTTP ASGI** 下启动请求仍为 400，不签发预登录 Cookie、不生成 SQLite 状态或调用 IdP。
- 人为附加 `X-Forwarded-Proto: https`、RFC 7239 `Forwarded`、`CF-Connecting-IP` 或多跳 XFF 不能修复该冲突；不允许为了通过测试重新信任这些头。
- 合成内部 HTTPS ASGI 的对照正常生成固定 IdP 授权跳转及 `__Host-` Cookie；有合成 RSA token 的 HTTPS 回调可完成**隔离审核**，但不签发浏览器身份或记忆写入许可。
- 对于合法 start 生成的 Cookie/状态，内部 HTTP callback 返回固定的 303 安全终点与 Cookie 清除指令，**不调用 token exchange**。
- 真实 Uvicorn stack 明确 `proxy_headers=False` 的路径保留 HTTP；故意启用宽松代理重写的对照也因审核拒绝注入的转发头而不能将其当作安全解法。
- 候选实际 P3 supervisor 仍要求 `--no-proxy-headers`，并保留所有 OIDC OFF 闸门。

测试中的 `HTTPS` 是 ASGI 测试协议标记，**不是**对线上 TLS 通道的实测或代理证据。

## 生产修复方案的设计门槛（尚未实现）

不能简单删除 `scheme=https` 校验，不能设 `FORWARDED_ALLOW_IPS=*`、临时信任 `X-Forwarded-Proto`，也不能从 `CF-Ray` / `Rndr-Id` / `scope.client` 推出人类登录身份。

后续可评审两条互斥方案：
- **路径 A：受控、独立证明的可信传输边界。** 在 Render 入口之内有可靠且与请求绑定的证明，确定终止 TLS 的可信代理、它对转发头的清除/重写规则和受信源。再在单独审核过的边界中产出可信 `external_origin`，不让普通 headers 或 ASGI 客户端信息升级为权限。
- **路径 B：固定规范外部来源 + 独立认证。** 如果真实 Render 拓扑不支持导出上述可信来源，可设计一个默认 OFF 的 login-only adapter，只从受控部署配置构造固定公网 HTTPS origin 和固定回调域名，严格校验 Host/Origin、预登录 Cookie、一次性 state、PKCE、nonce、issuer/audience 与真实 IdP 签名。**绝不把 TCP IP/XFF 当用户身份**。但在安全审核前必须单独验证边缘 TLS、不可绕过的公网路由、别名域名/重定向、CSRF、Cookie Secure 以及正确的部署 commit，不能凭服务 URL 自己证明请求经 TLS。
- 两条路径都必须在真实 IdP、授权会话及用户同意/记忆写入接入前通过独立评审。方案选择和生产上线**均未在本 PR 做出**。

## 维持冻结

所有 PR 保持 Draft、未合并、未部署；真实 OAuth/IdP、浏览器会话签发、生产记忆写入和付费 OpenAI API 保持 OFF。v48 验收门槛 **36/36、108/108** 不变。

来源：
- Render: https://render.com/tutorials/web-service-vs-static-site/web-services
- Render: https://render.com/docs/web-services
- Render: https://render.com/docs/tls
