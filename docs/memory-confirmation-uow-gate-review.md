# 归汀 · 真实 MemoryActionUnitOfWork 内票据校验实验（只消费，不写记忆）

**独立 Draft、默认关闭、无 HTTP 路由、无实际记忆写入、无真实登录授权、无真实用户确认。** 叠加 PR #140。原有 MemoryActionLedger / MemoryActionUnitOfWork / HMAC 账本源码不改，v48 36/36、108/108 冻结范围不动。

新增 `backend/memory_confirmation_uow_gate.py` 与独立 `_memory_confirmation_uow_gate_worker` 合成测试。仅当三个实验标志 **全为字面 true** 才能执行：
- `MEMORY_CONFIRMED_UOW_GATE_ENABLED`（新增，默认 OFF）
- `MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED`
- `MEMORY_TRUSTED_AUTH_EPOCH_ENABLED`

## 本轮只证明什么

从现有后端 `MemoryActionUnitOfWork` 打开 SQLite `BEGIN IMMEDIATE` 后，**在同一个事务与同一数据库连接**检查登录主体、login_id、纪元摘要和撤销状态，票据 `pending`、120 秒到期时间、会话、request_id、预览摘要和确认文案。匹配后将票据标为 `consumed`，但 **不调用任何 MemoryActionUnitOfWork 的 canonical write / memory action / commit()**。测试有意令事务回滚，确认票据仍为 `pending`，同时 `memory_action_requests` 和消息表仍为 0；说明未来作者可将它作为相同事务内的校验钩子，而非两段独立的 SQL。

路径强校验限制 UOW 与票据数据库必须是同一绝对路径。无效认证、重登、跨账号/会话、错摘要、提前领取或默认关闭都拒绝。

## 尚未解决的实际工程阻断

**这不是生产级写入授权。** 无真正登录认证来源、无真人同意/CSRF/确认 UI。当前没有把这个组件放入真实 `MemoryActionEntryBackend.remember` 事务，也没有验证从确认、HMAC ledger claim、canonical insert 到 commit 的完整流程和中断恢复；不能宣称“撤销与真实记忆写入已经安全串行化”。新的返回值 `CheckedInTransaction` 不构成写入凭据，不可在独立事务外使用。

在 #138–#140 最新 CI 实际通过前，本分支也不得合并。独立用例仅用合成数据，不调用网络或付费 API。
