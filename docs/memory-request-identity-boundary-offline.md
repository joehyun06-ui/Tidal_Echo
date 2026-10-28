# 归汀 · 请求级用户身份认证边界（默认拒绝 / 离线）

新 Draft，堆叠于 PR #145，**无 HTTP 写入路由、无生产认证服务、无真用户数据**。

## 已确认的现有阻断

`backend/app.py:1169-1176`：`check_auth` 只有单用户共享 `RELAY_SECRET` 的 Bearer（也支持旧 URL query token）。因此仅能证实持有 relay 密钥，**不能推出任何独立的 principal/login/授权用户**。既有 `/app/memory/session-write/preflight` 是 GET 只读 dry-run，只能确认服务器目前有活跃会话，同样不是保存许可。

## 这轮默认拒绝的模块

新增 `MemoryRequestIdentityBoundary`：没有显式的**请求级、独立验证过的 per-user authenticator** 以及持久登录纪元检查器就一律拒绝，不解析 `Authorization` 内容为 principal，也不从请求 query、Header、Cookie 或 `request.state` 猜用户。未来必须由具备真实登录会话的 middleware/composition root 注入可信 provider。当前仓库没有提供这种已验证的 provider，测试中使用的都是合成回调，不可作为 production evidence。

即使 provider 被合成地注入，仍要求精确 HTTPS Origin、单个 CSRF Header、`Sec-Fetch-Site: same-origin`、严格的 POST/JSON/路径、无任何 query、无伪造 identity Header、scope 与服务器 canonical 会话匹配、SQLite 当前 epoch 未撤销；任何一个失败/开关运行时改变就拒绝。返回 `RequestIdentityReviewResult(accepted_for_review=True, may_write_memory=False)` 是**非权限预检结果**。

## 之后必须完成

- 确立真实身份提供者与登录生命周期、session cookie/token 安全设计，并做真实 HTTP 登录/退出/会话切换集成测试；
- 由真实 UI/服务端提供可验证的用户交互事件（#145 当前仅合成），并确定 CSRF double-submit/server secret 实现；
- 通过独立身份、事件、审计、确认票据与同一个 UOW 构建经审查的写入许可，防 token 重放/撤销/审计不一致；
- 评审生产审计 key 管理、留存/删除和流量限制；在这些未通过前禁止启用记忆写入。

原有 relay 和 GET preflight 未修改，v48 36/36、108/108 不变；本分支只运行离线合成 ASGI Request 测试、无网络或付费 API。
