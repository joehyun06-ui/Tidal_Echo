# 归汀 Stage48 — P1 恢复设计、分级授权与脱敏验收门禁（仅方案 / SYNTHETIC）

## 0. 范围与权威基线

**本阶段没有新授权读取 P1 生产文件内容、打开 P1 SQLite、获取文件名清单、读取 Render 环境值/密钥、导出数据或创建数据库备份。** 仅复用 Stage47 用户提供的脱敏文件元数据、当前生产版本公开的仓库源码，产出恢复方案和**完全合成数据**的逻辑测试。不得把方案创建当成已恢复。

- Render 生产：`ouou-home-telegram` LIVE `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`，自动部署 OFF，1 GB 持久磁盘 `/var/data`。本阶段不部署、合并、回滚、启用新服务、改变 Telegram 或执行快照 Restore。
- Stage42–44：主 SQLite `relay.db` 真实一致性备份、Windows 验证及旧→新代码隔离启动已通过（operator-observed）；不等于所有数据库的备份通过。
- Stage46：授权的 6 个附件与 1 个 Loop 配置文件真实 Windows DPAPI 加密转存及隔离校验通过；备份依赖当前 Windows 用户/系统，不可直接作为异机可移植灾备。
- Stage47：用户通过认证 SSH 在原有 Render 服务上完成**无正文**盘点：已知 `codex-generation.db` 文件 90,112 bytes，三个固定 SQLite sidecar 当时未发现；`codex-home` **71 files、34 directories、7,995,043 file bytes，truncated=false**；剩余 15 个匿名根目录项中 **11 个仅满足 SQLite 扩展名候选规则、4 个目录**。没有证明其中 11 个为有效数据库，也没有证明没有隐蔽密钥。
- 本轮依旧保持正式 v48 **36/36 / 108/108** 门槛冻结；本 Stage48 为独立灾备审计编号，不构成 v48 正式验收阈值调整。

## 1. 源码依赖图与风险

```text
[relay.db]  <-canonical message ids--  [codex-generation.db]
   |                                    |
   +-- message attachments --> uploads  +-- jobs/leases/session/thread ids
                                         |
                               [codex-home] (auth + cached/opaque state)
                                         |
                                 [codex-workspace]

[Usage Ledger: only if explicitly enabled]
  GUITING_LEDGER_MODE -> GUITING_LEDGER_PLAN -> storage.ledger_db
  separate continuation/fingerprint signing keys / pause/intent markers
```

1. `backend/codex_generation_store.py` 独立 `codex_generation_schema`、`codex_sessions`、`codex_generation_jobs`（并容许特定 compaction 表）；任务关联 canonical relay message ID、SHA-256 digest、job lease、状态和 thread/cwd。源码定义多种执行中状态，例如 `thread_dispatching`、`in_progress`、`callback_pending` 和 `dispatch_uncertain`。**单份 SQLite 恢复不等于跨库消息引用、回调幂等及未完成任务安全。** 恢复后必须先禁用 Codex worker / provider 自动重试，不得直接重新启动正在处理的队列。
2. `backend/codex_generation_runtime_config.py` 可把 Generation store 配到 `CODEX_GENERATION_DB`，默认是 `/var/data/codex-generation.db` 且不得等于 relay.db；只有运行门禁启用才准备路径。**磁盘文件存在不证明生成服务实际启用**。
3. `backend/deployment_config.py` 配置 `CODEX_HOME`、`CODEX_WORKSPACE`；Codex Home 很可能混有鉴权状态、会话缓存和其他私有资料。不能将 71 个文件批量归类为可传输普通业务资产。
4. `backend/usage_ledger_entrypoint.py` 的 MODE 默认 disabled，但运行时状态尚未观察。仅在非 disabled 时读取 `GUITING_LEDGER_PLAN`，从 `storage.ledger_db` 取得实际路径，并依赖 continuation/fingerprint key；`usage_ledger_secret_files.py` 对私有密钥权限有额外约束。**不能从未知的 11 个文件扩展名猜 Ledger 所在位置、启用状态、密钥存储或可恢复性**。
5. `backend/usage_ledger_paths.py` 将 `-wal/-shm/-journal` 定义为 SQLite 文件家族的一部分。即使 Stage47 一次性没发现 sidecar，也不能在生产活跃时直接 `cp` 一个 SQLite 主文件或声称文件家族一直不变。

## 2. 数据类与发布门禁

| 类别 | 只读证据 | 当前结论 | 后续真实动作前必须单独获得的授权 |
|---|---|---|---|
| 明确的 `codex-generation.db` | 固定路径 + 大小、仅元数据 | **PRESENT_UNVALIDATED** | 批准本 DB 的应用一致性备份范围、受控加密目的地、隔离内容验证和保留策略 |
| 11 个 SQLite 扩展名候选 | 只有数量，没有文件名、头信息、Schema | **EXTENSION_CANDIDATE_NOT_VERIFIED** | 先单独批准隐私保护的身份识别和有限内容读取；**不批准批量复制** |
| Codex Home | 71 files / 34 dirs / 7,995,043 bytes | **METADATA_COMPLETE_CONTENT_UNCLASSIFIED** | 单独批准仅在本机分类“凭据/可重建缓存/不可重建状态”，不公开名称和原文；再逐类审批 |
| Usage Ledger/签名密钥 | 源码条件依赖，生产状态未知 | **UNKNOWN_CONFIG_DEPENDENT** | 单独批准只回传脱敏启用状态；后续计划/签名密钥的安全保存、迁移与验证需独立授权 |
| 未分类 4 个目录 | 根目录聚合元数据 | **NOT_IDENTIFIED** | 先仅进一步做匿名分类，不能整体下载 |
| 全服务恢复 | 无隔离全栈重建证据 | **NOT_TESTED** | 独立隔离环境及网络、RPO/RTO、安全回调设计和全面恢复审批 |

## 3. 建议的后续执行顺序（全部是未来可提案步骤，不是当前授权）

**G0 — 当前，方案设计：PASS / 不接触 P1 生产内容。** 使用 Stage47 脱敏摘要进行恢复差距分析；合成 CI 不能将候选数自动升级为数据库数，不能将元数据自动升级为备份/恢复成功。

**G1 — 后续需要新授权，未知库身份核查。** 如需辨认 11 个候选，先在受控操作员环境为每个固定文件建立本地临时不公开的映射，再只向验收输出分类合计。不得向 GitHub/聊天输出真实名字、SHA、路径、表名或任何密钥；文件名后缀只作候选。是否要打开 SQLite 文件头、只读 PRAGMA 或 schema，都属于**读取文件内容**，必须在额外授权后再做。检查失败应保持 UNKNOWN，不得推测为 Ledger/缓存/有效 DB。

**G2 — 后续需要新授权，单独备份 Codex Generation SQLite。** 先清点 job 活跃状态/业务重试风险并核实数据库路径实际配置（只交付脱敏枚举，不交付私有路径），获批后使用 SQLite 在线 Backup API 创建事务一致副本，限制临时文件权限、留存和网络路径，采用 SSH 加密传输至仅业主受控私有存储。不能使用未经验证的在线 `cp`、忽略 WAL 的 `immutable=1` 或直接改动原始 DB。校验 `integrity_check`、`foreign_key_check`、独立 Schema/版本，以及所需消息 ID 在对应 `relay.db` 快照中的关系。**不同时间点的 relay/codex 备份不是天然的原子跨库快照**；缺少对应消息、任务租约和回调幂等时必须标记 INCOMPLETE。

**G3 — 后续需要新授权，Codex Home 分离。** 在不泄露文件名/正文的前提下，由操作员先判断凭据、账号令牌、会话状态、缓存、线程工作树和不可重建资料，必须默认采用高敏感等级。禁止一键批量同步 `codex-home` 到 GitHub、CI、聊天或云盘；对未鉴别项保留 `UNCLASSIFIED`。恢复时不能自动加载既有认证凭据或对外联系服务；应先验证清单与所有者确认，再单独处理。

**G4 — 后续需要新授权，Usage Ledger。** 仅在批准的脱敏环境核对 mode 是否为 disabled/history/capture，之后才确定是否需要计划、独立 Ledger DB、私有签名密钥和 intentional-coverage 记录。不得为了盘点去启用功能、读取/展示密钥、伪造账本连续性或自动重放漏记时段。

**G5 — 后续需要新授权，隔离恢复与最后发布门禁。** 使用临时恢复副本、断开外部网络、禁用 Telegram、LLM、Codex、记忆写入、定时任务。先以只读检查关联与排队状态；所有不确定任务均封存为需要人工重对账的状态，不自动重新派发。完整生产灾备必须再覆盖附件/配置/独立数据库/必要的 Codex Home/已确认 Ledger、权限、凭据轮换和 RPO/RTO；Render 原生快照不支持跨盘克隆，不应在原盘执行有破坏性的恢复演练。

## 4. 脱敏输出与安全边界

- 本 Draft 的 `scripts/render_stage48_p1_recovery_decision_offline.py` 是**纯内存决策器**：没有读文件/DB/环境值、网络、进程或备份 API；只能在 `GUITING_STAGE48_SYNTHETIC_ONLY=true` 下运行内置虚构样例，不能传入任意真实文件路径或生产 JSON。
- `evaluate()` 只接受 Stage47 白名单元数据键、固定数据库名和匿名桶计数；拒绝额外的 `filenames`/`paths`/`secret_value`、布尔数值冒充计数、统计不自洽，以及有内容读取、写入、备份或恢复迹象的证据。
- 即使输入 `result=PASS_METADATA`，输出始终 `result=DESIGN_ONLY`、`real_p1_restore=NOT_TESTED`、`authorizes_export_or_backup=false`、`unknown_valid_sqlite_confirmed=0` + `unknown_valid_sqlite_confirmation=NOT_TESTED`。这里的 **0 是已确认有效未知 SQLite 的数量，不是已证明不存在的数量**。
- 合成 GitHub Actions 仅验证这些拒绝行为，使用虚构计数和标签。不运行真实 Render SSH，不读取 Codex 身份材料，不要求用户上传任何已有备份。
- 元数据变化、部署版本变化或新的授权均必须作为下一轮独立证据记录；不能沿用本 Draft 自动外推许可。

## 5. 本阶段验收结论

只可标记 **P1 恢复设计与门禁模型 PASS（若 CI 通过）**。不得标记 P1 独立库“已备份/恢复”、11 个扩展名文件“有效 SQLite”、Codex Home“已处理”、Ledger“已启用”或完整生产灾备“已通过”。生产冻结、所有现有 Draft 的未合并状态、v48 正式 36/36 与 108/108 验收阈值均不变。
