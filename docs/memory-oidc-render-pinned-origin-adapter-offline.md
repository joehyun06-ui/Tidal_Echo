# 归汀 · 固定外部 HTTPS Origin 与内部 HTTP 的隔离适配演练（OFF）

本阶段堆叠已经验收的 Draft #165（29/29 工作流、56/56 CI jobs，Linux Python 全回归 2007/2007）。#165 稳定复现了 Render TLS 卸载后的合法浏览器 HTTPS 请求进入 Uvicorn 时具有 HTTP `scope.scheme`，而原有独立 OIDC 审核仅接受 HTTPS `scope.scheme` 的兼容性阻断。**本 PR 没有修改旧审核器、没有修改生产 `backend.app`、`backend.p3_relay_app`、`render_start_p3.py` 或 Render 服务。**

## 新增纯离线适配层

`backend.memory_oidc_pinned_origin_adapter_offline.SyntheticPinnedOriginAsgi` 只允许包裹现有三路由的 `create_isolated_oidc_asgi_review()`，接收：

- 固定且严格格式的 `public_origin=https://app.example.test`（必须与受控 OIDC 回调绝对 URL 匹配；仅为合成示例，并非真实域名配置）。
- 合成内部 ASGI `expected_internal_server`，精确匹配 `scope.server`；不通过用户请求头猜测“可信对端”。
- 测试用、独立于请求头的 `synthetic_proof_provider()`；必须严格返回 Python `True`，且需要连续校验，空值、`1`、失败或中途撤销一律拒绝。**这不是不可篡改传输证明，不存在真正的生产实现。**
- 独立审核环境变量 `MEMORY_OIDC_PINNED_ORIGIN_REHEARSAL_ENABLED` 默认 OFF。它遵循 #158 的 Render supervisor 上线闸门，在真实启动配置中被判定为未经审核的 OIDC 激活开关而拒绝。
- 仅允许 HTTP 内部 ASGI 和三条审核路径。要求未伪造的精确 Host，Origin 若存在只能为固定 HTTPS 地址；对重复 Host/Origin、`X-Forwarded-*`、`Forwarded`、`X-Real-IP`、`CF-Connecting-IP`、`CF-Ray`、`Rndr-Id`、Authorization 和伪造身份头均不授信、直接阻断。仅保留原始头和原始 server/client，复制新作用域时把 `scheme` 设置成合成 `https`，交给现有独立 OIDC 审核。
- 合法模拟请求的授权目标仍是受控 IdP URL，`redirect_uri` 固定，`__Host-` 预登录 Cookie 有 Secure/HttpOnly/SameSite=Lax/Path=/ 且无 Domain；模拟回调由原有 state、PKCE、RSA 令牌签名审核、nonce 与单次状态消费逻辑处理。最终只跳转审核完成页面，**不签发用户浏览器身份、不授权记忆写入**。

## 27 项离线负向与正向测试

`backend.tests._memory_oidc_pinned_origin_adapter_worker` 在 Windows/Linux 使用真实 Starlette OIDC 审核、真实 `httpx.ASGITransport` 和 `uvicorn.Config.load()`，但**不监听 TCP、不访问 Render/IdP、不使用任何生产密钥**。验收：

1. 未开审核开关或关闭真实网络时拒绝；证明缺失、`False`、`None`、伪真值 `1` 或二次检查被撤销时拒绝，不产生 SQLite 或预登录 Cookie。
2. 内部 HTTP 通过测试证明后获得固定 IdP 303 和安全预登录 Cookie，使用手动模拟的 Browser Secure Cookie 完成无权限 OIDC 代码/状态审核；真实浏览器的 HTTPS cookie 是否能在 Render TLS 卸载链路正确流转**尚未验收**。
3. Host、Origin、重复头、伪造代理字段、Cloudflare/Render 跟踪头、身份头、URL 返回地址劫持、非 HTTP scheme 或不合成的 server 均拒绝。Uvicorn 代理头中间件保持禁用的路径测试正常；错误启用并配合伪造 XFP 不获得豁免。
4. 保留原始 ASGI scope，隔离应用不返回用户身份凭据或 memory writer 权限；测试检查生产入口没有导入该适配器。

## 为什么不能直接生产上线

此适配器只能说明，**如果**未来存在可信、与请求绑定的 TLS 边缘来源证明，固定规范外部 HTTPS URL 的兼容性问题可以通过隔离适配层解决，且不需要盲目信任用户提供的 `X-Forwarded-Proto`。但是测试桩无法证明某次真实请求经过 Render HTTPS 入口，不能仅靠 `Host` / `Origin` / `scope.server` 证明传输安全，更不能把常量 True 当成真实证明。

在允许任何生产适配器前，还必须：
- 独立审核当前 Render ingress 的不可绕过性，真实边缘与应用的关系，来源证明是否与**每个请求**关联且不可被客户端冒充；对边缘头的清洗/覆盖方式、别名域名、TLS 终止、Host/Origin/CSRF/cookie 浏览器兼容性取得证据。
- 确认生产启动仍使用未上线的 `render_start_p3.py` 安全契约，且候选 HEAD 与最后 live SHA 不存在未经批准的分支漂移。
- 完成真实 IdP/JWKS、真实用户同意与审计、会话存续和撤销、记忆写入权限的独立安全验收。
- 不得删除既有 `scheme=https` 校验、设置 `FORWARDED_ALLOW_IPS=*`、复用 `scope.client` 或插入无条件 `lambda: True` 作为认证证据。

当前 `ouott / ouou-home-telegram` 最后已审计部署提交仍为 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`，自动部署关闭。本 PR **只增离线适配器、测试、CI 和文档**，继续保持 Draft、未合并、未部署；真实 OIDC 登录、记忆写入及付费 API 均 OFF，v48 验收门槛冻结 **36/36 和 108/108**。
