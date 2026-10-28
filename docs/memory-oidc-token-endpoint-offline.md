# 归汀 · 固定 OAuth Token Endpoint 线协议审查（离线 Draft）

堆叠已验收 Draft #149。新增 `PinnedOidcTokenExchangeReview` 仅通过**显式注入的离线传输器**构造并校验 OAuth authorization_code 请求/响应；没有 HTTP 发送器、没有实际 IdP、没有服务端回调或生产登录。完整的真实联网安全性、TLS/DNS/SSRF/代理/超时和生产 OAuth 客户端认证不在本轮证明范围内。

请求使用仅固定配置的 HTTPS token_endpoint、callback redirect_uri、client_id，`POST application/x-www-form-urlencoded`，包含 `grant_type=authorization_code`、`code`、`redirect_uri`、`client_id` 和原有 S256 `code_verifier`。该模块不发起网络访问，不从客户端 URL、`jku` 或动态 issuer 指定端点。

对纯合成的 Token Endpoint 响应，验证 HTTP 200、完全相同的 final URL（禁止假设跟随重定向）、JSON MIME、`Cache-Control: no-store`、`Pragma: no-cache`、响应正文大小限额、重复响应头/JSON 键、非法响应类型、`access_token/token_type/expires_in/id_token` 严格结构。仅返回未经信任的 ID Token，**不返回、打印或持久化 access_token**；由 #149 完成原子 `state` 消费，随后 #148 完成 RS256 签名与 nonce 检查。客户端收到 HTTP 302、错误页、OAuth error、畸形 JSON、任何额外 refresh token 均拒绝。

测试采用真实 Python bytes/form 解码、内存生成 RSA 签名和注入式 Mock transport，验证 token 请求格式、响应结构、安全失败及整个 #149→Token wire boundary→#148 的离线组合流程。没有使用真实 API Key，未修改公共 relay、生产路由或记忆 UOW，所有功能开关默认 OFF，`may_issue_browser_session=False`、`may_write_memory=False`。

**仍需独立评审的上线阻断：** 真实身份提供商的 OAuth Client 注册（选择 public/confidential 及客户端认证方法）、HTTP 发送器的 TLS/代理/重定向/DNS 保护、浏览器首次登录 state 绑定和回调 `iss` 防 mix-up、敏感密钥处理与 IdP JWKS 轮换、持久 nonce 与浏览器凭据发行衔接、审计和真实用户交互。参考 RFC 6749 §4.1.3、RFC 9700。保留 v48 正式 36/36、108/108 冻结，不合并、不部署、不允许真实用户记忆保存或付费 API 调用。
