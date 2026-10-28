# 归汀 · 离线真实 SQLite 记忆原子提交演练（Draft / OFF）

叠加在 **PR #141 (59322fc4)** 上。只增加一个内建 Python 私有实验方法 `MemoryActionEntryBackend._remember_confirmed_offline_review`；**不新增 HTTP、插件或前端路由，不接真实登录服务，不触碰用户数据、不部署、不合并。**

## 验证内容

- 复用现有 MemoryActionEntryBackend 的规范化、原有操作权限投影、HMAC 请求账本、canonical user message、来源证据、记忆写入和事务提交实现，不复制业务写入流程。
- 仅在六个显式 ON 条件同时具备时允许运行（实际默认全 OFF）：`MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED`、`MEMORY_SESSION_BOUND_WRITE_ENABLED`、`MEMORY_SESSION_CONFIRMATION_ENABLED`、`MEMORY_TRUSTED_AUTH_EPOCH_ENABLED`、`MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED`、`MEMORY_CONFIRMED_UOW_GATE_ENABLED`。
- 私有实验方法固定使用原有 operator_cli → web/relay 可信内部投影（**不是浏览器授权**），要求请求绑定明确活跃会话、正常敏感度、≤1000 字且已经规范化。它在原有 UOW 的 **BEGIN IMMEDIATE 取得写锁之后**校验并原子消费 pending 票据，再在该**同一** UOW 进行 HMAC ledger claim、canonical insert、记忆来源处理、终态和 COMMIT。
- 事务成功：票据 consumed / canonical / memory / HMAC ledger 一起持久化；后续同票据不能再用。新的显式票据、相同 request_id 在既有签名账本内只返回 replay，不重复创建消息。
- 事务异常或提交前禁用开关：所有更改回滚，票据保持 pending，不能出现单独消费、写入一半的状态。锁内提交与其他连接的 logout/revoke 串行化。
- 任何未接入原 UOW 的一阶段 ticket.claim() 仍是独立离线实验；**不作为**本方法的授权准备步骤。本方法只接受 pending 票据。

## 保留的上线阻断

此次证明仅针对合成测试的正常 SQLite 事务路径。**没有**真人确认（固定确认短语不证明点击）、实际身份服务、CSRF/origin 防护、操作审计、HTTP/插件写入路由或真实用户权限。还未证明进程在 COMMIT 前后崩溃、结果未知的跨进程恢复与审计，也没证明任何线上部署/跨磁盘/多副本数据一致性。

不要把这份内部可调用方法当作可以开放给前端的安全能力。正式提供用户功能前必须补齐实际身份来源、签名授权、用户交互证据、事务不确定结果处理、安全审查和独立端到端验收。

## 验收命令

`python -m unittest backend.tests._memory_atomic_confirmed_worker -v`（Windows 和 Linux 独立运行）。
`python -m unittest discover -s backend/tests -p "test_*.py"`（冻结 v48 基线不变）。
