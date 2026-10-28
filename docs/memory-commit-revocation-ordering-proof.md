# 归汀 · 撤销和最终写入的事务排序审查（合成证明，不可上线）

叠加在 PR #139 上的**测试与设计审查 Draft**。不引入生产 API，不修改冻结 `MemoryActionUnitOfWork`、HMAC 账本、真实记忆、前端或付费账本。

## 问题

PR #139 用 SQLite `BEGIN IMMEDIATE` 原子领取票据，并在同一事务内检查登录纪元；但下一步真正的 `MemoryActionUnitOfWork` 会另起一个 `BEGIN IMMEDIATE`。如果中间发生 logout / revoke，在旧协议中仍可能已经领票、随后依旧写入。**单独领票成功绝不等于允许提交**。

## 本轮验证的*有限*不变量

新增独立测试 `backend/tests/_memory_commit_ordering_worker.py`：先通过 PR #138 / #139 的现有模块以**合成主体和合成票据**进行准备和领取，再对**同一个 SQLite 文件**进行一次 `BEGIN IMMEDIATE`。在这个事务中同时核对：
- 原子领取后票据确为 `consumed`、仍未过期，且同一主体、登录 ID、认证纪元、请求 ID、会话、内容摘要一致；
- 纪元未撤销；关闭任一默认关闭的实验标志会拒绝；
- 仅在核验通过后向 `memory_commit_ordering_probe_v1` 写入一个不包含任何记忆正文的**合成标记**，作为可回滚提交的证明。

覆盖：logout / 重新登录 / 跨账号拒绝、同一事务独占锁时撤销等待、撤销先完成时拒绝、事务异常回滚、重复模拟写入唯一约束、错误数据库路径拒绝。

**不证明**：真实内存或 SQLite MemoryActionLedger 的安全提交、跨进程写入结果恢复、崩溃后不可重新提交、真身份验证、用户确认 UI、CSRF 防护。合成 marker 不可被误读为实际记忆已保存。旧票据仍被服务器确认接口禁用，任何真正写入前还需要在已有账本事务内做同数据库、同事务的可信纪元/会话/票据状态二次校验，并完成幂等与崩溃审计。须证明完整事务生命周期里没有另开连接造成竞争，也不能用两段普通 SQL 检查替代同事务检查。

## 验收

`python -m unittest backend.tests._memory_commit_ordering_worker -v`

Linux 与 Windows 独立 CI 仅跑合成测试；CI 未成功之前不可标记本轮验收通过。先复验上游 PR #138 和 #139；全部保持 Draft，不合并、不开启真实记忆写入或部署。
