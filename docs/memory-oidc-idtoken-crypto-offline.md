# 归汀 · 固定公钥 OIDC 身份签名验证（离线 Draft）

叠加 #147。实现 `PinnedOidcIdTokenVerifier`，仅用于离线、默认关闭的密码学验收。

当前仓库没有实际 OIDC/OAuth 授权码登录、PKCE、重定向、nonce 发放/一次性消费、真实 IdP、动态可信 JWKS 管理；**本代码不是完整登录系统**。验证器接收已通过外部受信流程取得的 ID Token，验证由服务端配置固定的 issuer/client ID、公钥 kid/RS256 签名、sub、aud、nonce、iat/exp/auth_time/nbf。旧 relay 共享 Bearer 绝不授权用户身份。

公钥只接受显式注入的 2048/3072/4096-bit RSA 公钥 PEM，禁止从 token header 的 `jwk/jku/x5u` 网络取钥，拒绝 `none`/HS256、未知 kid、重复 JSON key、时钟异常、超时、过期、未来认证、nonce 不匹配、浮点/布尔时间字段。多受众/azp 的真实 IdP 语义需要在确定 IdP 后另行评审：本子集暂拒绝它们，避免误授权。

**安全输出限制**：`VerifiedOidcSubject` 只证明一次 JWT 签名及声明匹配，其 `may_issue_browser_session=False` 与 `may_write_memory=False`。它不会调用 #147 的会话发放器；对真实用户身份，仍需绑定 OAuth code + PKCE 与服务端 nonce 原子一次性消费，防 replay/state 注入，再配置 IdP 和密钥轮换。签名有效不等于人类同意，也不等于本地登录。

测试 RSA 私钥在 CI 内存中临时生成，不写任何代码库或日志。本模块依赖 PyCA `cryptography`，目前由现有固定依赖间接引入；若生产使用，应独立固定此依赖的版本并通过供应链审核。本 PR 不启用 HTTP、生产密钥、付费 API 或实际记忆写入。v48 正式 36/36、108/108 基线不变。
