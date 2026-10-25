# 归汀 · 服务端会话写入预检（实验草稿）

状态：**Draft PR #135；未合并、未部署、默认关闭，不具备真实写入能力**。此 PR 依赖 PR #134（新记忆事务绑定）与 PR #133（只读记忆证据）的草稿代码，不修改正式 v48 36/36、108/108 基线。

## 新增的只读路由

| 层 | 请求 | 认证 |
|---|---|---|
| relay | `GET /app/memory/session-write/preflight` | 必须在 **Authorization: Bearer** 请求头，拒绝 URL query token |
| loop | `GET /loop/memory/session-write/preflight` | 必须提供既有 `X-API-Loop-Internal-Token` |

两个服务都要求：
- `MEMORY_CORE_ENABLED=true`；
- `MEMORY_SESSION_WRITE_PREFLIGHT_ENABLED=true`（**默认关闭**）。

**注意**：`MEMORY_SESSION_BOUND_WRITE_ENABLED` 是另一个独立的写入实验开关。预检本身不打开这个开关，不能发起实际写入，也不会创建任何记忆对象。

只有 loop 内部从 `load_config()` **显式读取** `active_session`，并与 `session_rows()` 核对；不复用聊天界面的 `active_session_id()`，因为它会在活跃会话缺失时回退到最后一个会话。预检必须拒绝这种推断。请求无法指定、覆盖或枚举目标会话。返回的唯一成功结果：

```json
{
  "schema": "guitting-session-write-preflight-v1",
  "mode": "dry_run",
  "active_session_verified": true,
  "would_write": false
}
```

响应带 `Cache-Control: private, no-store`，不包含用户记忆、会话 ID、密钥、提供商消息或写入请求 ID；relay 必须完全匹配这个固定契约，否则拒绝结果。

## 安全和产品边界

- 这是**单用户现有 relay Bearer** 边界的验证，不是用户/租户级授权体系，也不能授权未来写入。
- 预检响应仅代表检查时刻服务端配置中存在活跃会话，不可当成可重用授权票据，不产生能力令牌、不存储预检状态。
- 不接受浏览器指定的 `session_id`、`scope_ref`、canonical 消息、角色/来源、请求体或 URL query 参数。
- 后续真实写入必须使用**同一请求上下文的服务端授权主体和会话**、同一事务的来源绑定，并单独实现防 CSRF / 重放 / 同意确认、内容校验与多租户隔离。单靠这个预检不能实现上述安全条件。
- 本分支没有绑定前端按钮、没有开启任何 UI 自动写入，也没有动真实用户数据库。
- 旧来源缺少 canonical 会话绑定时继续被只读面板隐藏，不能猜测回填。
- 以前一次性付费 API 账本不重放、不清理。无 OpenAI API 调用和真实用户记忆。

## 离线测试

```powershell
py -3.12 -m unittest backend.tests._memory_session_preflight_worker -v
```

测试涵盖：缺少或错误令牌、默认关闭、活跃会话不在服务端会话表、拒绝 query token、阻断浏览器指定会话、上游返回可写能力或额外会话字段、无数据库读写、固定来源契约。

Linux/Windows 独立工作流必须均通过后，才可以把本 PR 计作代码级预检验证成功；即使通过也不能代表正式生产部署。
