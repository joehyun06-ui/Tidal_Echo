# 归汀 · 固定 HTTPS 外部来源的真实浏览器 / TLS 卸载离线验证（OFF）

本阶段堆叠 #166 已验收的 Draft 安全链：30/30 工作流、58/58 CI Jobs、Linux Python 回归 2007/2007。此前 #165 证明 Render 边缘 HTTPS 终止后传递内部 HTTP 与 OIDC 审核要求 HTTPS ASGI 的冲突；#166 实现**仅用于演练**的 `SyntheticPinnedOriginAsgi`，但当时只在 HTTPX ASGITransport 合成请求中测试。本轮开始检查**真实浏览器的 Cookie、导航和跨站回调行为**。

## 浏览器和模拟部署拓扑

- 直接复用仓库已有 `_LoopbackOnlyProxy`、`_make_ephemeral_certificate`、`_SyntheticTwoSiteApp`、Playwright 浏览器启动和回调断言，不重写任何已通过验收的真实浏览器基础逻辑。
- 真实 Chromium 和 WebKit 发起 `https://app.example.test` 与 `https://login.identity.test` 导航；每次只能通过监听 `127.0.0.1` 的 CONNECT 测试代理转接到 `127.0.0.1` 上同一个临时 TLS 测试服务器，**不解析这些域名，也不访问公网主机**。
- TLS 证书在临时目录中现场生成且自签名；浏览器测试环境主动跳过该证书的信任链校验。因此**没有验证真实证书链、Render 生产边缘证书或其可信来源**。已有 #155 独立测试负责本地证书链的安全拒绝逻辑。
- 新增测试夹具 `_SyntheticTlsEdgeToHttp`：浏览器到 Uvicorn 的外侧连接是真实 TLS/HTTPS，但经精确域名和审核路径过滤后，夹具复制 ASGI scope，模拟 Render TLS 卸载，把**测试应用看到的内部 scheme 改为 HTTP**。再由 #166 的固定来源适配器在测试证明为真的条件下把复制的 scope 恢复为审核逻辑所需的 HTTPS；原始请求头不改动。
- 被控的 `synthetic_proof_provider` 只是可切换 True/False 的**测试开关**，并不证明浏览器所用的某次 TLS 连接真实来自 Render，也不具备防篡改性。不可作为生产身份或代理授权，不能变成 `lambda: True` 在真实服务中启用。

## 新增真实浏览器验收

`backend.tests._memory_oidc_pinned_origin_real_browser_worker`，Chromium / WebKit 各运行 7 项：

1. 浏览器真正保存 `__Host-gt-oidc-review`，其域名只绑定 application host、`Secure`、`HttpOnly`、`SameSite=Lax`，JavaScript 的 `document.cookie` 无法读取；跨站顶级 GET 回调时浏览器携带该 Cookie，合成 RSA token/state/PKCE 审核成功，但**不签发用户登录或记忆写入权限**，最终 Cookie 被清理。
2. 模拟证明缺失时，从实际浏览器发起的启动请求不获得 Cookie/授权跳转；模拟证明在返回 IdP 页面后被撤销时，回调拒绝 token exchange。
3. 错误 IdP issuer、另一浏览器上下文重放、过期 Cookie、审核开关撤销的情况均不能兑换合成 code 或取得会话。
4. 测试检查 Browser HTTPS 在模拟边缘入口可观察到，且本地服务始终只在 `127.0.0.1` 监听，使用现有严格的 CONNECT 主机白名单。

**注意：** Proof 被撤销后的拒绝不会在离线适配层签发身份。即使演练中的单个回调以 400/404 结束，也不能证明生产浏览器端的完整失败清理体验已通过；相关设计仍需独立评审。

## 验收边界与上线阻断

本 PR 只添加测试、CI 和文档；**不修改** `backend.app`、`backend.p3_relay_app`、`scripts/render_start_p3.py`、`render.yaml`、原 OIDC 审核器或 #166 适配器。不会连接真实 Render、OAuth/IdP 或付费 API，也不会写入真实用户记忆。

本地浏览器链路成功，只证明固定 HTTPS Origin 与内部 HTTP 的**浏览器功能兼容性可被模拟**，不能证明真实 Render 代理对端、边缘转发头清洗、请求级信任证据、真实 TLS/域名白名单或线上部署 commit。真正上线前，必须独立取得可核验的入口可信边界证据，完成真实身份提供商与 Cookie/CSRF/Origin 端到端验收，并经正式合并与部署审批。

当前 Render `ouott / ouou-home-telegram` 上次只读核查的 live SHA 仍为 `a2d8699a`，不是本次 Draft。真实 OIDC 登录、用户同意、记忆写入与付费 API 保持 OFF；v48 正式验收 36/36、108/108 保持冻结。
