# 归汀 · 服务端浏览器会话凭据（离线 / 默认关闭）

堆叠在 Draft PR #146。确认的阻断：现有 `RELAY_SECRET` 是整个 relay 的单一共享 Bearer，**不是请求级用户认证**。本 PR 为独立登录后的凭据生命周期提供一个可复用的 SQLite 演练实现，但**没有实现独立账号密码、OAuth、OIDC、Passkey 或真实登录中间件**。合成 issuer 无法证明真人登录。

`BrowserLoginSessionRegistry` 默认 OFF；只有独立验证的登录事件提供者可以请求发放随机 256-bit 会话 token 和独立 256-bit CSRF token。临时 SQLite 只持有二者 SHA-256、用户与登录标识、epoch 哈希、服务器会话 ID、Origin 哈希、到期时间和撤销状态。它在 `BEGIN IMMEDIATE` 中与 `memory_auth_epoch_v1` 联查，避免会话发行/验证与同库纪元撤销交错；退出、轮换使旧 token 立即失效。

仅用 **模拟 Starlette Request** 进行双平台验证：严格的 POST、独立离线路径、HTTPS Origin 精确匹配、`Sec-Fetch-Site: same-origin`、独立 CSRF header、单个 Cookie header、拒绝 query 参数和自称用户身份 header。未安装 HTTP 路由。未来浏览器 Cookie 的 `Secure; HttpOnly; SameSite=Strict; Path=/`、`__Host-` 前缀与真实交互日志仍需单独设计与验证。

服务器的会话 provider 必须独立读取 canonical sessions，不使用前端传入的 scope/会话列表。其签发回调目前只有测试合成身份，**严禁拿共享 relay token 或构造的 `TrustedTicketContext` 冒充真实登录**。

返回 `BrowserSessionReview(may_write_memory=False)`；**不能传给记忆 UOW 或签名审计系统当写入授权**。真实身份认证器、完整注销授权、跨进程 Cookie 吊销、会话并发预算、密钥管理、生产 CSRF 端点、用户确认可验证事件及真实记忆写入授权链仍全部是上线阻断。此 PR 不修改正式 relay，不部署、不合并、不启用付费调用或生产记忆写入。v48 36/36 与 108/108 冻结门槛不变。
