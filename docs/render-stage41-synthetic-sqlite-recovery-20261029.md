# 归汀 Stage 41：完全合成 SQLite 恢复演练（离线、生产不变）

## 输入范围

- Render 已确认 LIVE 的代码基线：`58ecb255e5cb32e37ee0d949aaa7d8bff896d971`，autoDeployTrigger OFF。
- 使用 `backend.channel_store` 的正式迁移器与现有完整性校验逻辑，不创造生产数据表格式。
- 测试代码是 **unittest 工作者**，只使用 `tempfile.TemporaryDirectory()` 内部创建的虚构 SQLite 数据；不接受 operator 指定的 DB 路径，不能读取已有 `/var/data`、真实 Render disk、snapshot、聊天、Bot Token、用户、模型 API 或记忆。
- GitHub Actions 的 2 个 runner（Windows + Linux）模拟数据库创建、WAL 写入、安全一致性备份和恢复。CI 禁用真实外部网络并对关键运行时启用标志设置 false；生产不写。

## 完整性判据

每个有效恢复副本使用现有 `channel_store.connect_read_only` 打开，验证：

1. `PRAGMA integrity_check` 恰好一行 `ok`。
2. `PRAGMA foreign_key_check` 无错误行。
3. `channel_store.validate_core_schema_v1_v6(conn, require_relay_tables=True)` 不抛错。
4. `channel_store.validate_memory_operator_schema_v1_v11(conn)` 不抛错，以覆盖完整 schema 后续版本。
5. SQLite read-only/PRAGMA query_only 确认拒绝写入。

## 六个测试案例

- 真实 SQLite WAL 模式的**未检查点**写入由 `Connection.backup()` 生成一致备份；复制至另一个隔离路径，哈希相同，仍可完整验证。
- 备份时间点之后继续提交另一条虚构消息：源副本 2 条，恢复副本仍为 1 条，展示真实数据恢复时的 RPO/data-loss 边界，不宣称零丢失。
- 模拟错误的主 `.sqlite3` 文件单独复制，故意缺少未检查点 WAL 中最新提交，与一致备份比较，必须检测到不完整。
- 故意删除必要的 `delivery_parts` 表，原 schema validator 必须拒绝。
- 故意插入不满足 FOREIGN KEY 的数据，检查器必须拒绝。
- 故意损坏 SQLite 文件头，检查器必须拒绝；且只读打开应禁止写入。

**所有负面案例都在虚构的临时副本上执行**，与用户真实消息完全无关。仅采用 SQLite 的应用级 `backup()` API；这不等同于验证 Render 的磁盘快照格式或复制时一致性。

## 验收条件

- Windows、Linux 6/6 子测试通过，原始执行日志中有 `Ran 6 tests` 和 `OK`。
- Stage 41 Draft HEAD 的完整 GitHub Actions 结果全部 success；默认 Python 回归也成功。
- 只读核实生产仍保持 LIVE、autoDeploy OFF，未发生 restore/rollback/合并。
- 只对**合成数据恢复流程**宣布 PASS；真实 Render 快照内容不可直接从这个单元测试推断恢复成功。
- 真实恢复能力、历史备份可靠性、旧版本 schema 回退与 RPO/RTO 数值依然未获证明；下一阶段需要明确批准的非生产隔离环境和一致的生产数据副本处理策略。

正式 v48 `36/36`、`108/108` 冻结，不修改 Telegram、OIDC、记忆和付费 API 开关，也不触发新生产消息。
