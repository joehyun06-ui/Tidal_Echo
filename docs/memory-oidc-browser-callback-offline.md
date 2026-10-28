# 归汀 · OIDC 浏览器发起/回调绑定（独立离线 Draft）

叠加 PR #151，纯合成 ASGI Request 离线测试。不修改正式 HTTP 路由，开关 `MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED` 默认 OFF；真实 OAuth 交换器、会话签发和记忆写入依然不存在。

## 受信边界

浏览器**发起**：仅接受 HTTPS、固定 Host、`POST /__offline_review__/oidc/start`、精确同源 `Origin`、`Sec-Fetch-Site: same-origin` 和 JSON 类型，不接受用户身份头、relay Bearer、查询字段或预先设置的 Cookie。为当前流程随机生成 256-bit 临时浏览器 nonce，服务器利用运行态 HMAC 密钥签发带时间戳和固定 issuer/redirect 的 `__Host-gt-oidc-review` Cookie。未来真实 HTTP 响应须设置 `Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=180`，不得设置 Domain；建议对发起和回调加 `Cache-Control: no-store`。

浏览器**回调**：仅接受固定 HTTPS Host 和回调路径的 GET，Query **恰好**为 `state`、`code`、`iss`；重复参数、错误发行方、混入 error/伪造用户信息和错误临时 Cookie 都在执行 Token Endpoint 交换前拒绝。按 RFC 9207 精确比较解码后的发行方 `iss`，阻止 OAuth mix-up。回调是从 IdP 导航过来的，因此不要求同源 Origin 或 `Sec-Fetch-Site: same-origin`，但它需要浏览器在跨站顶层导航发送 `SameSite=Lax` 的 cookie；一切成功仍必须有 #149 SQLite 持久绑定的单次 state、#150 限定 token 响应，以及 #148 pinned RSA ID Token 验证。

本轮 HMAC Cookie 只证明拿到服务器颁发的**匿名预登录**浏览器值，不证明具体真实用户。cookie 一次性许可来自 #149 state 绑定；签名、时间和 issuer 不能单独颁发本地会话。结果类型 `OidcBrowserCallbackReviewResult` 显式禁止 `may_issue_browser_session` 和 `may_write_memory`。

**上线阻断：** 真实安全的浏览器路由和 Cookie 传输、Cookie 覆盖/并发登录、可信 HTTPS 反向代理部署、IdP 元数据支持 iss、生产登录会话生成与吊销、OAuth code+PKCE 的受控真实交换、密钥托管轮换、完整 UI/CSRF/审计、人工同意。仍不合并、不部署、不联网、不允许真实记忆保存；v48 正式 36/36、108/108 门槛不变。参考：https://www.rfc-editor.org/rfc/rfc9207。
