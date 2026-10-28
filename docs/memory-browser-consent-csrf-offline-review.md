# 归汀 · 浏览器用户确认 / CSRF 安全审查（离线独立 Draft）

叠加在已验收的 #143 之上。新增 **默认关闭** 的 `backend/memory_browser_consent_review.py` 与独立 unittest worker。**不会连接任何实际 HTTP 写入路由，不接真实记忆账本，不构成正式浏览器登录或真实用户许可**。

## 现有边界

当前 `/app/memory/session-write/preflight` 是只读 dry-run，受已有 `relay_app.check_auth` 保护；其 token 并不自动证明“具体哪位真实用户”或真人同意。现有 `MemoryAuthEpochRegistry` 是对已认证身份的撤销登记，认证来源尚未接好。不能据此宣称具备多租户用户认证。

## 新增隔离验证

- `TrustedBrowserReviewContext` 由**可信、请求级服务端认证组合根**传入（本轮仅使用合成 provider），同时提供 `TrustedTicketContext`、服务器会话的 CSRF token 和确定的受信 origin；不接受客户端声明的 principal、login_id、auth_epoch。
- 准备/确认两阶段均重新核对 origin 精确匹配、非 URL/query token 的 CSRF token 常数时间比较、服务器活跃会话、SQLite 持久登录纪元。HTTPS 默认，HTTP 仅限明确 loopback。
- 最多 8 个待确认、120 秒过期、32 字节随机 nonce；内部只存哈希/请求摘要/身份约束/过期时刻，不保留预览明文。带 nonce 的回执不可被记忆写入系统使用。
- 确认必须是严格布尔 `True`、确认短语完全匹配、预览内容 SHA-256 完全匹配。重放、并发重复、过期、跨账号、登出、换会话、换登录代次、换 CSRF、错误 origin 都拒绝。
- `clear_for_logout` 可清除进程内临时待确认条目，但不能替代数据库登录纪元撤销。
- **这仍然不是 HTTP 客户端真实“点击确认”的证据**：客户端字段 `confirmed=True` 不能单独证明人类操作；必须由未来经过单独评审的 UI/服务端交互和审计流程绑定。进程内 nonce 不可跨进程复用，不可独立提供安全写入权限。不要把回执接入 #142 的写入路径。

## 生产上线阻断

必须先设计并实现真实认证中间件、请求级 session 和 origin/CSRF 验证、用户实际确认流程及签名审计、授权撤销竞争、跨进程持久确认记录、速率限制和错误处理。新增模块只是数据隔离、负例和接口边界检查。任何真实写入标志仍默认 OFF。不改 v48 36/36、108/108 基线，不合并、不部署、不调用付费 API。

验收：`python -m unittest backend.tests._memory_browser_consent_review_worker -v`（Windows/Linux）。
