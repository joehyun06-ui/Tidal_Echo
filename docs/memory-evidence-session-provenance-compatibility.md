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

不从 SQLite SELECT 记忆文本、memory_key、原始消息文本或完整 meta；不返回任何逐条 ID、原文、会话名、真实来源 ID、哈希或凭证。单次最多扫描 2,000 个候选，超限拒绝；用于未来经过授权的**本机只读检查**，不是浏览器 telemetry。当前只在虚构数据上运行测试，没有读取用户真实数据库。

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
