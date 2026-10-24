# 会话记忆证据：实验性只读 API

状态：**独立功能分支，未启用、未部署、非生产级检索质量认证**。

访问路径：
- 浏览器 -> `GET /app/sessions/{session_id}/memory/evidence`（P3 relay，复用 relay 登录令牌）
- relay -> `GET /loop/sessions/{session_id}/memory/evidence`（复用内部 `X-API-Loop-Internal-Token`；仅内网）

必须同时显式启用：
- `MEMORY_CORE_ENABLED=true`
- `MEMORY_EVIDENCE_UI_READONLY_ENABLED=true`（**默认关闭**）

后端在内部校验 session 位于 canonical `session_rows()`；使用 `channel_store.connect_read_only` 打开现有 SQLite，不创建数据库，不执行任何 INSERT/UPDATE/DELETE。所有读取限制在 `scope_type=session`、`scope_ref=session_id`、`status=active`、`sensitivity=normal`；来源必须关联同会话真实的 canonical message、真实事件及用户角色。没有来源时不返回记录。上限 10 条、8,000 字符、每条 20 个来源。所有请求无模型调用和 Embeddings。

对应前端草稿 PR：[`ouo-home-ui#32`](https://github.com/joehyun06-ui/ouo-home-ui/pull/32)。返回格式：`guitting-memory-retrieval-ui-v1`，`mode=server_readonly`，`api_session` 绑定请求路径，`views` 为已验证当前会话 active 记忆的只读**列表**。每项 `answer` 只是已保存的 `normalized_content`，并非针对用户问题生成的答案；`as_of` 是最后确认日期，不是按日期回溯检索。`source_ids` 来自确实存在的 `memory_sources.id`，作为审计引用 ID，不是可直接打开的链接。

重要限制：
- **没有**完整时间版本回溯、冲突推理、复杂检索或真正的语义搜索。仅展示当前会话、当前有效、非敏感且可验证的记录。
- 授权仍是现有单人 relay token；不是多租户身份体系。不要将此路径开放给新用户、多租户或不具备会话级权限的服务。
- 已有真实 API 冒烟测试与合成多记忆规则测试不代表本端点已通过线上验收。
- 需要在临时/预览环境单独核对内部转发、CORS、负向身份访问、SQLite schema、缓存策略及负载行为。
- 未改动 v48 冻结验收基线，不触碰线上主分支，未批准合并或部署。

测试：`python -m unittest backend.tests.test_memory_evidence_readonly -v`；对应 CI 运行全部 `backend/tests`。
