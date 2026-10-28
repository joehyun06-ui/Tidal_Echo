# 归汀：新会话记忆的两阶段确认（实验评审）

**状态：独立 Draft、默认关闭、没有 HTTP 路由、没有生产启用。**

这个实验基于 PR #135 的只读预检以及 PR #134 的事务绑定；新增 `backend/memory_session_confirmation.py`，但不修改冻结 v48 36/36、108/108 门槛，不触碰真实数据库和既有付费 API 一次性账本。

## 阶段 A：准备确认预览（仅进程内）

仅在 `MEMORY_SESSION_BOUND_WRITE_ENABLED=true` **且** `MEMORY_SESSION_CONFIRMATION_ENABLED=true` 时允许。后者默认关闭。

经服务端授权的未来组合根负责构造 *typed* `RememberExplicitMemoryRequest`（与原有 HMAC 账本格式一致），并提供一个**可信、不可由请求 JSON 覆盖的** `session_provider()`：返回明确配置的服务端活跃会话以及 canonical session rows；不得复用聊天用的「缺失时退回最后一个会话」逻辑。

`prepare(request)` 先核对会话和 scope，严格限制 `normal` 敏感级别、最多 1000 字符且必须为已规范化的内容；产生随机一次性票据、需要用户确认的原文预览和内容/会话/请求 ID 绑定摘要。**准备阶段不连接数据库、不创建 ledger claim、不进行记忆写入、不开外网。** 只在内存中保留待确认请求，最多 8 个，120 秒过期。

## 阶段 B：显式确认 → 原有 SQLite 事务

`confirm(ticket, content_sha256, confirmation_text="CONFIRM MEMORY")` 接受匹配的摘要和明确确认文本。票据使用前**先从内存池取出并删除**，避免并发双写；无效文本/摘要、过期票据、服务端活跃会话切换、会话删除、功能关闭均安全拒绝。确认后仍须调用 PR #134 的 `remember_explicit_user_memory_for_server_session`，使用原有的事务性 canonical 用户消息、来源证据、HMAC 终态和请求 ID 幂等账本。

如 commit 过程抛错，该票据仍不可重试；后续只能由有权限的运维按原 request ID 审计已发生的账本状态，再走新的用户确认。没有自动循环重放或删除账本。

## 必须明确的安全限制

- **没有把任何写入开放为 HTTP 或插件操作**，没有浏览器确认按钮，也没有真实用户确认凭证。协议中的固定确认文本本身**不能证明一个人确实点击同意**；这一点必须由未来前端/relay 的授权和交互验收单独证明。
- 当前单用户共享 Bearer token **不是**多租户/角色/会话授权。仅提供 `session_provider` 函数和会话存在性检查，不能把这个实验解释为解决了实际用户授权问题。
- 同一 Python 进程中的票据不持久化、重启即作废；多 worker / 多副本不得在未实现共同的原子票据存储与身份绑定之前启用。无过期后台轮询，清理仅在新 prepare 时发生。
- 准备预览会包含用户即将保存的文本；任何将来的 API/日志/监控对接必须新增最小化传输、保护缓存与内容泄漏审查。票据和内容不应进入 URL/query。
- 票据摘要用于**预览一致性**，不是授权签名；真正的持久化防篡改来自原有 SQLite ledger HMAC。浏览器可以机械传回“CONFIRM MEMORY”，它不能替代确认 UI、CSRF 防护或实际身份验证。
- 遗忘、修正、旧记忆来源回填、多用户、离线多进程共享、真实生产故障注入/崩溃一致性、内容检索仍不在此 PR 范围内。
- 不得合并、部署或打开开关，直到身份与会话授权审查、明确确认 UI 和跨进程幂等/故障恢复机制另外完成验收。

## 独立、合成数据测试

`python -m unittest backend.tests._memory_session_confirm_worker -v`

验证：准备阶段零写入、严格确认、错 digest/确认语、过期、重复提交、并发竞争、服务端会话切换或删除、正常敏感级限制、容量限制、功能关闭失效、失败后不可重放，以及实际 SQLite 端到端来源展示与旧 HMAC ledger 的同 request ID 幂等语义。CI 继续保留原 Linux 全量单元测试与 Windows 独立专项测试。

**正式范围：** PR #32 前端显示、#133 只读证据、#134 内部新写入、#135 认证只读预检以及本实验都是独立草稿链；没有生产合并。用户原有 Windows 本机离线 PASS 归属于先前固定提交，不能被当作本实验的本机验收。
