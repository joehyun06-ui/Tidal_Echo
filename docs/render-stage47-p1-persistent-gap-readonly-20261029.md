# 归汀 Stage47 — P1 持久数据的只读差距分析与匿名分类

## 基线与授权

- 2026-10-29 Render `ouou-home-telegram` LIVE 于 `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`，自动部署 OFF，1GB 单实例磁盘挂载 `/var/data`。
- Stage45 用户 SSH 只读盘点观察到：`codex-generation.db` **90112 bytes**，Codex Home **至少** 14 个文件、9 个子目录、7304438 bytes（深度限制导致部分结果）；匿名未分类根项 15 个，其中 11 文件、4 目录。Stage46 已独立验证 6 附件及 Loop 配置恢复。
- **本轮仅允许源码分析、合成 CI、用户可执行的只读元数据调查。** 绝不读/复制独立数据库正文、Codex Home 文件、潜在 API 凭据或身份凭据，不读取环境变量，不把未分类原始文件名发往 GitHub/聊天，不创建非生产或付费资源，不使用 Render disk Restore。
- Stage42 主 SQLite + Stage43/44 跨版本隔离启动 + Stage46 附件配置，只覆盖已经核实的资产；P1 尚未通过任何实际备份与恢复。

## 代码溯源及风险分类

| 资产 | 源码证据 | 含义 | 现在可下的结论 |
|---|---|---|---|
| `/var/data/codex-generation.db` | `backend/codex_generation_runtime_config.py` 默认 `CODEX_GENERATION_DB`；`backend/codex_generation_store.py` 使用 SQLite、保存独立任务/会话/调度状态 | 单独 SQLite family，存在 SQLite -wal/-shm/-journal 侧文件的可能；不包含在 `relay.db` 备份中 | Stage45 只确认文件存在/大小；**未检查数据库内容、活跃队列、完整性、迁移或恢复** |
| `/var/data/codex-home` | `backend/deployment_config.py` CODEX_HOME；`codex_generation_runtime_config.prepare_generation_paths` 只有显式相关能力启用时准备路径 | 可能包含凭据、缓存、历史或线程状态；目录有多层级 | Stage45 仅达到深度限制的部分统计；不可假设可忽略，也不可直接打包上传 |
| `/var/data/codex-workspace` | CODEX_WORKSPACE，条件运行时持久目录 | 源码/工具工作区，可能存在独立不可重建数据 | Stage45 观察到空目录，但只是当时的元数据快照；不能推断永远为空 |
| Usage Ledger SQLite、plan 与 key | `backend/usage_ledger_entrypoint.py` 中 `GUITING_LEDGER_MODE` 默认 disabled，若启动则读取 `GUITING_LEDGER_PLAN`，并使用 plan 中 `storage.ledger_db`；`usage_ledger_secret_files.py` 独立严格权限的密钥文件 | 独立账本及可用于验证签名的密钥可能影响恢复语义 | 源码默认关闭不等于已确认实际生产状态；**未读生产环境变量、plan 或密钥**，不能猜真实文件名或位置 |
| 15 项匿名未分类根目录 | Stage45 元数据实测 | 可能有多个额外数据库、日志、配置、密钥或未识别子目录 | **未授权读取文件名与内容**；先只按扩展名和类型进行匿名桶统计，绝不导出文件 |

**重点：** 一份存在的独立 SQLite 即使只有 90KB，也不能通过 `cp` 证明数据库一致性；如果未来批准备份，必须使用 SQLite 在线 Backup API（或其他明确支持的事务一致性方法），且与主库恢复版本、调度状态一致性单独验收。Codex Home 可能混有认证材料，与可迁移业务资产必须先分类隔离。

## Stage47 匿名元数据盘点程序（无需真实数据上传）

脚本：`scripts/render_stage47_p1_metadata_readonly.py`：

1. 默认根目录只能是 Render POSIX 的 `/var/data`，必须显式 `--perform-readonly`。POSIX 扫描以 root 目录描述符起步，用 `O_DIRECTORY | O_NOFOLLOW` 逐层打开 Codex Home，只读 `os.stat`/ `os.scandir` 元数据，拒绝遍历软链接及跨设备挂载，不打开文件内容和数据库，不读取任何环境变量值。
2. Codex Home 的文件数、子目录数、符号链接数、其他节点数与累计文件字节数严格有限：20,000 条目、深度 12、约 8 秒预算。命中任一限制、子目录不可读或路径更换，都标注 `PARTIAL_METADATA`、`truncated=true`、固定类别原因，不将部分统计冒充总量。
3. `codex-generation.db`、三个预期 SQLite sidecar 只报告固定类别的 `present/missing/wrong_type` 和字节数；**不读 SQLite schema 或打开 DB**。
4. 根级未分类项保持 Stage45 已知路径排除规则，只输出目录/链接/SQLite 扩展名、SQLite sidecar、JSON 配置扩展名、秘密文件扩展名候选及“其他文件”的**数量**。**绝不输出未分类原文件名、路径、SHA256、内容、文件/目录列表或密钥值**；扩展名候选仅为不确定分类，不能证明是否含凭据。
5. `--synthetic-root` 必须显式设置 `GUITING_STAGE47_SYNTHETIC_ONLY=true`，仅供跨平台合成 CI；Windows 合成路径不代表生产 POSIX fd 无竞态安全保证。
6. 仅报告 `contents_read=false`、`environment_values_read=false`、`file_names_emitted=false`、`source_writes=false` 作为**工具设计边界**，不将这些输出视为独立的 OS 级审计证明。
7. 本阶段只统计，不解析 `GUITING_LEDGER_PLAN`、不执行 SQLite PRAGMA、不读取 Cookie/token/日志/私钥、不复制生产目录、不改变任何源状态。不能从统计数量自动给出备份许可。

## 合成 CI 及判定

`.github/workflows/render-stage47-p1-metadata-offline.yml`：Ubuntu、Windows 只使用虚构文件树，验证匿名输出和不存在内容泄露、独立 SQLite/sidecar 状态、Codex Home 遍历、符号链接不追踪、深度限额导致 PARTIAL、synthetic gate。CI 通过仅证明程序合成逻辑；**真实 Stage47 元数据调查要等业主自愿运行并提供脱敏 JSON**。

之后单独授权才能继续真实 P1 数据备份：
- Codex Generation DB：应用一致性备份 + 隔离恢复 + 生成任务状态机一致性，禁止在线直接文件拷贝。
- Codex Home：区分不可重建业务状态与凭据/缓存，身份材料不能随业务数据公开/无加密迁移。
- Usage ledger：先确认启用状态和合法私有计划（不回显参数），再批准确定路径范围。
- 未知项：仅经数据所有者逐项明确范围后处理。

**本阶段验收仅为“来源驱动的恢复差距分析 + 匿名元数据工具合成测试”。** Render 原生快照异盘恢复、整体服务重建、RPO/RTO 继续未测试；v48 **36/36、108/108** 冻结，所有 Draft 未合并、不部署。
