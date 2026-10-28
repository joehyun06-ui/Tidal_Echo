# 归汀 · 原子记忆提交的崩溃与应答丢失恢复（离线 Draft）

堆叠 PR #142。新增 **默认关闭且无生产调用方** 的不确定提交结果核验：旧记忆入口与原 `MemoryActionUnitOfWork`、HMAC 账本的公开行为不变。

## 本次防护

在实验性 `MemoryActionEntryBackend._remember_confirmed_offline_review` 发生 `transaction_outcome_uncertain` 后，原有 `_lookup_existing_terminal(binding)` 继续负责验证签名账本及记忆语义；**另在同一个恢复 UOW 写锁中** 使用 `MemoryUowConfirmationGate.assert_consumed_for_uncertain_in_existing_uow` 验证票据已持久化消费、主体、登录代次、会话、request_id、内容摘要、确认短语和 `consumed_at < expires_at`。缺票据、未消费、过期时才消费、当前纪元撤销、被关闭开关、签名终态不一致时 **拒绝回报成功**。

已提交与未提交的回滚状态严格区分。提交后超出 TTL **不应否认** 在 TTL 内完成的原消费；检查原消费时间，而非响应时间。

## 强制退出与测试

新离线 worker 使用 `os._exit(61)` 在实际子进程中分别于确认票据更新、canonical 消息插入、HMAC 请求账本完成和 SQLite COMMIT 之后直接终止，无 finally/正常释放模拟，依赖 SQLite crash 回滚或保留原子结果。父进程新开连接查询 SQLite 票据、消息、账本、记忆数量，并验证安全重试。父进程作为**原进程死亡后的独立恢复进程**，利用现有 `_resolve_uncertain` 和 HMAC 签名账本，在同一个新事务内进一步核对已消费票据；提交前硬退出应报告不确定且不得编造成功，提交后硬退出应可只读重建终态。撤销后应拒绝透露恢复结果，TTL 在已提交后自然过去不应否认原先及时完成的消费。

另使用合成异常模拟提交应答丢失、提交前回滚、不可变 HMAC 账本阻止篡改、票据篡改、logout 竞争、过期回执、开关关闭。

**限制：** 子进程强退不等于断电或文件系统 fsync/power-loss 验证；不能推出磁盘持久性保证。没有真实登录或真人同意/浏览器 CSRF/UI/权限审计；生产用户写入仍关闭。尚未做到跨机器重复提交恢复或正式的用户授权证据绑定。不合并、不部署、PR 保留 Draft。

运行：`python -m unittest backend.tests._memory_atomic_recovery_worker -v`。
