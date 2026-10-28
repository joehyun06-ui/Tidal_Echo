# 归汀 · Host / Origin / Cookie / OIDC 回调重定向专项（默认 OFF）

本轮堆叠在 #162（26/26 工作流、50/50 Job，Python 2007/2007）。不接入线上 Render，**不新建生产登录路由、不会签发用户会话、不读取真实身份数据、不进行记忆写入**。针对现有的 `backend.memory_oidc_isolated_asgi_review.create_isolated_oidc_asgi_review` 与 `OidcBrowserCallbackOfflineReview`，通过真实 `httpx.ASGITransport` 向 Starlette app 发送离线 ASGI 请求。临时 SQLite、RSA 测试密钥和 IdP 签名均为合成数据；无需真实 TLS 连接或 OAuth 授权。

## 单元 / ASGI 集成验收

Windows/Linux 分别执行 `backend.tests._memory_oidc_host_origin_cookie_worker`：

- 登录启动要求固定的精确 `https://app.example.test` Origin、`Sec-Fetch-Site: same-origin`、无旧 Cookie 且无 URL query；恶意/重复 Host、Origin、端口变体、混合大小写、反向代理改写声明、模拟身份头均拒绝发放预登录 Cookie。
- 授权服务器跳转只能指向被固定的合成 `https://login.example.test/authorize`；`redirect_uri` 仍指向固定 `https://app.example.test/callback/oidc`，不得反射 `next=https://evil.test` 或其它请求自带返回地址。
- 预登录 `__Host-` Cookie 必须具备 `Secure`、`HttpOnly`、`Path=/`、`SameSite=Lax`，绝不出现 `Domain=`；此 Cookie 只负责一次性 state 绑定，不能作为身份登录凭据。
- 正常的 IdP 跨站**顶级 GET 导航**无需同源 Origin；合成 RS256 token/state/cookie 验证后也只返回 **303 到站内固定 `/__offline_review__/oidc/done`**，清理预登录 Cookie。
- 失败的 callback 即使带有错误 Host、Origin、转发头、身份头、重复 Host/Cookie、错误协议或附加恶意 `next`，也不得调用 token exchange；终点重定向不得泄露 `state`、`code`、合成 subject、身份密钥。正常与错误回调均发送固定 Cookie 删除指令（`Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax`）。
- `done` 页面拒绝含 query 的请求且不签发 Cookie；所有安全响应有 `Cache-Control: no-store` / `Referrer-Policy: no-referrer`，审核功能默认 OFF。

## 不能由这轮推出的结论

1. 这里的 HTTPS 是**进程内 ASGI scheme**，不是 Render 实际 TLS 卸载与可信代理的验证；真实边缘对 `Host`、`Forwarded`、`X-Forwarded-*` 的处理仍没有取得独立证明。
2. 回调地址、Cookie 安全标记及合成 RSA 通过并不构成生产 OIDC 登录完成。真实 OIDC 路由**没有挂入** `backend.app` 或 `backend.p3_relay_app`，没有签发浏览器登录凭据；未来仍须审查 proxy peer attestation、host allowlist、源站/回调 HTTPS、真实 IdP/JWKS、浏览器兼容性、用户同意、记忆写入门槛。
3. 真实 Render 最后已确认 live 提交为 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`（2026-10-23）。以上代码都只存在于 Draft、未合并、未部署的安全链上，不能声称在线生效。

冻结 v48 正式验收门槛 **36/36、108/108** 不变。真实 IdP、浏览器身份、生产记忆写入和付费 API 保持 OFF。
