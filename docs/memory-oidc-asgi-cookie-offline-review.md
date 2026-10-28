# OIDC：独立 ASGI Cookie/重定向集成验收（仅离线）

本阶段叠加 PR #152，并在独立的 `backend/memory_oidc_isolated_asgi_review.py` 模块提供显式工厂 `create_isolated_oidc_asgi_review`。**它没有在 `backend.app` 中导入或挂载，不存在生产 URL 路由。** 唯一预期调用方为基于 `httpx.ASGITransport` 的测试客户端，无公网请求。

## 合成 HTTP 链路

- `POST /__offline_review__/oidc/start`：精确 HTTPS Host、同源 Origin、Fetch metadata、JSON `{}` 且请求体最多 16 字节；由 #152 生成签名预登录 Cookie + #149 持久化单次 state；返回 **303** 至固定身份提供商授权地址。该重定向在测试中**不跟随**，不访问真实 IdP。
- `GET /callback/oidc`：浏览器跨站顶层导航携带 `SameSite=Lax` 的 `Secure; HttpOnly; __Host-` Cookie；#152 验证固定 issuer `iss`、state/cookie 和 URL；#149 消费 state；#148 验证合成令牌的 RSA 签名和 nonce。**无论校验成功失败，均返回 303 到不带 OAuth 查询的本地固定结果路径，并清空临时 Cookie**，不会输出身份、令牌或会话许可。
- `GET /__offline_review__/oidc/done`：固定文字明确提示仅离线预检，无用户登录或记忆写入；任何查询参数都拒绝。所有响应都加 `Cache-Control: no-store`、`Pragma: no-cache`、`Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff` 和禁止脚本/框架的 CSP。

默认关闭开关 `MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED`，并要求 #152、#149、#148 的离线安全开关为真；只要实际 HTTPS 发送器或真实联网显式授权开关启用，本 ASGI 测试应用就拒绝运行。这使 in-memory 集成无法因为测试配置误连真实 OAuth 网络。没有可外部调用的 production ASGI 组合根。

## 范围与仍需验收

验证的是 **in-memory ASGI HTTP Cookie jar、表单/请求头、303 响应与 SQLite 单次 state**，并非真正的 Chromium/WebKit 浏览器 Cookie/SameSite 行为、真实 TLS、可信反向代理、身份提供商、授权码真实交换或已登录用户。请求结果不签发 #147 会话，也不能触发记忆写入。生产登录前仍需实际浏览器跨站跳转、Cookie 安全头/Host、登录异常、限流、密钥轮换和可信 IdP 端到端测试。

所有 PR 保持 Draft、未合并、未部署。v48 正式 36/36、108/108 冻结，不发真实付费 API，不读写真实用户记忆。
