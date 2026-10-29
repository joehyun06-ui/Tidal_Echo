# 归汀 Stage46 — P0 上传附件与 API Loop 配置：恢复方案、合成演练（不碰生产数据）

## 基线与已知事实（2026-10-29）

当前 Render 服务 `ouou-home-telegram` LIVE `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`、auto deploy OFF，磁盘 `dsk-d9dk0gbrjlhs73aph4ig` 的 `/var/data` 为 1GB。Stage45 业主经 SSH 运行只读元数据清单，真实观察到：

- `/var/data/uploads`：6 个普通文件、464897 字节，无目录/软链接（仅当时扫描）；实际文件名、内容、引用一致性均未读取。
- `/var/data/api_loop.config.json`：普通文件，3118 字节；正文、敏感性未读取。
- `/var/data/relay.db`：主 SQLite 已在 Stage42 创建业务一致性备份，并在 Stage43/44 证明备份副本能被旧版及当前版后端初始化；但 **不能据此证明上传附件和 Loop 配置已经备份**。

## 风险与恢复对象

1. `backend/app.py` 的 `save_upload_bytes` 把附件按新生成的 `<prefix>-<token>.<ext>` 存在 uploads 下，返回 `/uploads/<stored>` URL；消息元数据也会引用这些文件。只恢复 SQLite 而缺少对应文件，会造成历史引用断链。**保留原存储文件名非常重要**；重新生成不同文件名不能保证历史链接有效。
2. `examples/api_loop.py` 从 LOOP_CONFIG 文件解析路由配置；`backend/deployment_config.py` 的 `validate_loop_config_file()` 会验证 JSON 与 MVP `main_chain` 约束。
3. **特别敏感**：`main_chain` 的每个条目可以包含 `key` 字段，意味着真实 `api_loop.config.json` **可能含明文 API 凭据**。不允许在日志、CI、GitHub PR、聊天、公共下载、普通网盘中输出此文件正文，不能把它当一般无密钥 JSON。
4. Stage45 发现的 Codex Generation 独立 SQLite、Codex Home 和未知根目录项目仍属于 P1，不纳入本轮 P0 操作；禁止把整个 /var/data 目录打包带出。
5. 2026-10-29 主数据库私有备份时间与附件/配置的实际备份时间可能不同。恢复演练需要评估**跨文件时间点一致性**，尤其是在生产运行期间新增/删除的附件；不得以“某时刻附件都复制完成”推断与旧数据库所有引用完全一致。

## 本 PR 已提供的合成数据程序

`scripts/render_stage46_p0_synthetic_restore.py` 和 `backend/tests/_render_stage46_p0_synthetic_restore_worker.py`：

- CLI **仅**在显式 `GUITING_STAGE46_SYNTHETIC_ONLY=true` 下接受绝对 synthetic-root；额外禁止直接使用 `/var/data`。数据只来自测试器生成的虚构 `synthetic-messages.sqlite3`、`uploads/att-fake.png` 和 `api_loop.config.json`。
- 验证测试资源为普通文件（拒绝 symlink/hardlink、子目录和不安全文件名），最多 128 个上传文件、总计 16MB。哈希在测试进程内部计算，不打印 hash/文件名/内容。
- 用 SQLite 只读打开合成库，仅读取**合成**消息中的附件 URL；验收恢复后同名文件可被查询到，且所有测试文件哈希一致，源文件哈希不变。
- 在独立临时目录恢复**合成副本**，使用与当前代码相同的 `deployment_config.validate_loop_config_file(..., render_mvp=True)`，校验配置格式。虚构模型配置字段 `key=FAKE-NOT-REAL-KEY` 绝不是真实凭据。
- 负面测试：附件缺失拒绝、无效 `main_chain.key` 拒绝、symbolic-link 文件拒绝、没有 synthetic-only 环境门禁拒绝。**不应从合成测试结论外推真实生产数据已复制**。
- 输出只有 `scope=SYNTHETIC_ONLY` 的脱敏状态；不创建网络服务，不访问 Render，不改生产，测试结束丢弃所有临时虚构数据。

## 后续真实 P0 验收需要另行明确授权

Stage45 授权是只读元数据。此前 Stage42 真实数据传输授权**只覆盖主 SQLite**。本 PR 不扩大授权范围。

未来若用户另行批准**仅这两个明确定义的生产资产**的安全处理，可设计独立、经代码审查的 Windows 操作员流程：

1. 只读确认 `uploads` 当前文件数、总字节数、各文件是否普通非链接、稳定性窗口；不要列出真实文件名或 hash 到对话。若附件仍被写入，需要考虑备份窗口中的并发变更并核验引用一致性。
2. 通过已有认证的 Render SSH/SCP，在**用户受控、加密磁盘且禁止云同步**的 Windows 私有目录，只复制 `/var/data/uploads` 的选定文件和 `/var/data/api_loop.config.json`。尤其要把 Loop JSON 当作**可能含明文密钥的秘密文件**，限制 ACL、明确短期保留、禁止 Git/CI/聊天上传。
3. 本机计算源文件与传输副本校验（真实哈希不发布），隔离临时恢复目录并验证附件文件名、所有可核对的历史引用、配置 JSON 与启动校验，以及副本清理；**不能**在联网隔离测试中使用真实 Loop 路由 key 触发付费 API。
4. 仅报告无内容、无名字、无 Key 的 PASS/FAIL/count 等；跨备份时间点缺失/新增必须标记为 **INCOMPLETE**，不能静默修复用户真实历史。
5. 独立评估真实配置密钥轮换和恢复后可用性，不得用 CI mock 伪装生产密钥真的有效。

## 当前判定

- Stage46 **合成软件恢复流程**：仅在 Windows/Linux CI 完成时可标为 PASS。
- Stage46 **真实 6 个附件、真实 Loop 配置备份与隔离恢复**：**NOT TESTED**，等待单独授权、设计审查、安全传输后才能执行。
- Stage45 P1 资产、完整灾难恢复、Render 原生快照异盘恢复：**NOT TESTED**。
- **禁止**真实快照 Restore、生产磁盘写入、Telegram 修改/消息、付费 API、合并/部署、CID/Key 输出。v48 正式 36/36 与 108/108 验收门槛不变。
