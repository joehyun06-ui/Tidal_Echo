# OIDC · Chromium / WebKit 跨站 Cookie 验收（离线 / 本机环回）

叠加 Draft PR #153；为 **浏览器真实 Cookie 引擎**增加独立于正式 Web relay 的 Playwright 验收。测试不实现实际登录，也不把合成 OIDC 身份赋予记忆写入权限。

## 浏览器与网络边界

- 使用 Python Playwright 1.64.1，GitHub Actions Linux 分别启动 Chromium 和 WebKit 的独立浏览器进程。仅在专门 CI 工作流中下载安装浏览器执行环境，不在正式依赖中添加 Playwright。
- 每个测试生成临时自签名 TLS 证书和 RSA 签名密钥，HTTPS 测试服务器只监听 `127.0.0.1`。测试浏览器配置一个只接受 `app.example.test:443` 与 `login.identity.test:443` 两个 **CONNECT** 目标的本机代理；代理永远只连接本机环回地址，不解析外部 DNS、不转发任何公网请求。
- `https://app.example.test` 与 `https://login.identity.test` 属于不同的 registrable site，能触发真实浏览器的跨站 Cookie 策略。请勿将测试域名错误地改回同一个 `example.test` 下的两个子域名。
- 浏览器对本机自签名证书设置 `ignore_https_errors=True`，**因此本测试不证明生产 CA 链、SNI 或公网 TLS 可信性**；这些仍为 #151 以后生产网络安全评审的阻断条件。

## 实际验证

浏览器从合成应用网页执行同源 JSON `POST`，由 #153 的独立 ASGI app 返回真正的 HTTP 303 与 Secure/HttpOnly/host-only/SameSite=Lax 预登录 Cookie。浏览器通过本地代理跳转到另一个站点的**模拟 IdP**，点击其授权链接以顶层跨站 GET 返回应用。应用通过 #152 Cookie + issuer + state 一次性绑定、#149 SQLite PKCE 和 #148 RSA 身份校验，最终重定向至不含 OAuth 查询的固定结果页，清除预登录 Cookie。

负例包括错误授权服务器 `iss`、签名 Cookie 被浏览器隔离、重放和 Cookie 清除、跨浏览器身份不可串用、Cookie 对页面脚本不可见。所有 HTTP 通道仅为本地回环；**不执行真实 IdP、真实付费 API 或用户记忆写入**。

## 未完成

没有正式路由、受信登录 IdP、已验证的生产 TLS、反向代理安全边界、浏览器真实用户身份登录/注销或用户保存记忆的人工同意。真实浏览器 CI 的 PASS 不代表登录链路可以上线；本分支保持 Draft、未合并、未部署，v48 正式 36/36 与 108/108 冻结不变。
