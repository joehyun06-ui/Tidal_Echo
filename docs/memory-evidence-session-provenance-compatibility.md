# 归汀 · 会话记忆来源兼容性决策（草稿）

状态：**仅设计与离线只读诊断。没有改动既有记忆写入路径、迁移生产数据、上线接口或启用功能。**

## 根因（代码核对）

`memory_explicit_actions.MemoryActionEntryBackend._run` 会调用 `uow._insert_canonical_action(text=...,metadata={"channel":channel,"source":source})`。这一历史写入协议**没有** `meta.api_session`。因此即使产生了 `scope_type=session` 的有效记忆，也可能缺少一个可由 canonical 用户证据独立验证的会话绑定。

实验只读接口 `GET /app/sessions/{session_id}/memory/evidence` 故意要求 scope、status、sensitivity、来源事件和 canonical 消息的 `meta.api_session` 同时满足条件。缺失时返回空结果是安全的拒绝展示，**不是**记忆已遗失或被删除的证据。

## 新增独立诊断模块

`backend/memory_evidence_coverage.py` 的 `summarize_session_coverage(connection, session_id, allowed_sessions)` 是**内部 Python 函数，不提供 HTTP/CLI 路由**。

仅分析已知会话内 `active + normal + session` 的记录，统计六种固定分类数量：
`verified_session_evidence`、`missing_session_binding`、
`different_session_binding`、`missing_qualifying_source`、
`content_over_display_limit`、`too_many_sources`。

不从 SQLite SELECT 记忆文本、memory_key、原始消息文本或完整 meta；不返回任何逐条 ID、原文、会话名、真实来源 ID、哈希或凭证。单次最多扫描 2,000 个候选，超限拒绝；用于未来经过授权的**本机只读检查**，不是浏览器 telemetry。**即使只有数量也可能透露用户是否存有记忆，仍属于需保护的元数据**，只能在本机明确授权下查看，不能对外暴露或自动上报。当前只在虚构数据上运行测试，没有读取用户真实数据库。

## 兼容性处理原则

1. **禁止猜测性回填**：不能仅凭 `scope_ref`、文本相似度或消息时间推断 canonical 消息属于某个会话。
2. **冻结 v48 不变**：当前既有显式写入不改动；新会话来源绑定应在另一份经安全审核的 opt-in 写入方案中设计，默认关闭。
3. 将来新写入必须由**已鉴权的服务端会话上下文**传入会话 ID，并在写入事务内绑定 canonical 消息及证据；不能信任浏览器随意传来的会话标识，也不能事后改写不可变证据事件。
4. 旧记录仅在存在独立、可靠、可审计的同期证据时才考虑关联；否则保持不展示。即使来源不合格，也不得悄悄创建替代的“可信来源”。
5. 人工审核与授权决定保留真实数据在哪里运行。禁止把用户真实 SQLite、API Key 或原文上传至 CI。

## 验收边界与现有证据

- Linux Python/前端 CI、Windows GitHub runner 专项测试已通过相应固定版本。
- 用户在自己 Windows 电脑上运行旧版固定源码 `f8ab8df8aca1268059436cb9e4fad38be92618ce` 与 `2036cb7f931582be604af01513d0ad4856b71ce9`，反馈验收脚本最终输出 `GUITTING_MEMORY_EVIDENCE_WINDOWS_LOCAL_OFFLINE_PASS`。这是会话中用户提供的本机运行截图；不是独立签名的审计日志。
- 新增覆盖率诊断需独立 CI；**不属于上述已通过的本机验收版本**。
- 没有生产环境实际登录/部署、没有真实用户记忆迁移、没有在线语义检索，也没有任何新付费 API 调用。
- 正式 v48 36/36、108/108 冻结门槛及旧的一次性付费 API 账本均未触碰。

## 混合来源必须整体拒绝

本分支曾有一项安全缺口：当记忆同时关联当前会话来源和其他会话来源时，原展示查询会只挑出匹配的一条来源，从而把整条记忆视为可信。

现调整为 **all-source fail-closed**：先枚举记忆挂载的全部来源（最多 20 条），每条都必须是 `messages.direction=in`、`messages.kind=user`、真实的 `memory_evidence_events`、`memory_sources.evidence_role=user`，且 canonical `meta.api_session` 必须为与查询会话相同的字符串。任何一条不合格、来源缺失、跨会话、不可验证或总数超限，都不展示该记忆。来源诊断使用同一条判定函数，混合来源计入 `mixed_or_unverified_sources`，不会错误归入“已验证”。

这会使某些原本可显示的旧记录现在**保守地不显示**。这是刻意缩小暴露面，不代表删除数据，也不是永久性的遗忘或写入机制改动。今后必须验证正式服务写入的新来源契约后再做选择性启用，不准直接放松该约束。

## 新写入会话绑定——独立 opt-in 方案验收门槛

尚未实施任何新写入。若实施，至少需同时保证：

- 会话 ID 来源于**已认证服务端的会话上下文**与服务端会话集合，不能直接信任前端传入的 `session_id` 或用户提示词。
- 本次写入的记忆作用域 `scope_ref`、canonical 用户消息 `meta.api_session` 和服务端当前授权会话完全一致；`channel/source` 仍须遵循现有类型白名单。
- `memory_action_requests` 的请求绑定摘要必须覆盖会话归属，防止同一 request ID 在不同会话重放；不得绕过现有去重、授权、抑制与遗忘语义。
- 新增写入默认关闭、单独功能分支评审，并证明异常中断、跨会话请求、角色伪造、时间回溯和一致性回滚均为安全失败。
- 不批量更新旧 `messages.meta`、不可变证据事件或历史 ledger；用户真实记忆只允许在明示授权的本地受审环境处理。
