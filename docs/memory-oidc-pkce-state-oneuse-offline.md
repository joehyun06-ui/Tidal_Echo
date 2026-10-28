# 归汀 · OIDC Authorization Code + PKCE + State/Nonce — 离线安全演练

基于 Draft PR #148，默认 OFF。**此代码不运行真实 OAuth、没有 HTTP 登录路由，不访问真实 IdP，不签发浏览器 cookie、不开放记忆写入。**

`OidcCodePkceOfflineReview` 为每次登录尝试生成 256-bit random `state`、256-bit `nonce`、RFC 7636 S256 PKCE verifier/challenge，并使用固定 HTTPS issuer/authorization/token endpoint/client_id/redirect URI 构建公开授权 URL。服务端必须通过独立受信的预登录 browser binding provider 提供随机绑定值（测试为合成），不能通过共享 relay Bearer、URL 查询字段或伪造账号 header 授权。

SQLite 在 `BEGIN IMMEDIATE` 中持久保存 state hash、绑定 hash、nonce hash、TTL 与加密的 nonce+verifier；使用仅运行时提供的 AES-256-GCM 密钥及固定配置的 AAD。授权回调先持锁验证 state、浏览器绑定、TTL 和密文完整性，然后原子标记 `consumed_at` 且清除密文并 COMMIT，**之后**才调用显式注入的纯离线 token exchanger，并复用已验收的公钥固定 RS256 ID Token 验证器。令牌交换/签名验证失败不允许重试相同 state，应重新开始授权流程。

模拟请求中的 `code` 不被自行解码成用户身份；只有实际验证的 ID Token 才可产生 `VerifiedOidcSubject`，且 `may_issue_browser_session=False`、`may_write_memory=False`。单次签名正确也不是完整 OIDC 登录。

**上线阻断：** 真正授权服务器、OAuth authorization code + PKCE 的 HTTP 交换（含 TLS、禁止重定向、超时、固定 endpoint、客户端验证和 token 响应解析）、预登录 Secure/HttpOnly/SameSite Cookie 与可信请求级绑定、state 回调去重、nonce 原子消费、用户体验、浏览器登录/登出生命周期、可审计用户交互和签名授权链。AES 密钥必须以生产秘密管理方案部署、轮换并保证跨实例可用；目前使用仅测试时运行态密钥。未经过独立评审不得接入 #147 会话签发。

所有 PR 保持 Draft、未合并、未部署。v48 正式 36/36、108/108 冻结基线不动，不允许真实用户记忆保存或付费 API 调用。
