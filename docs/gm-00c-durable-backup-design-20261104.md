# 归汀 GM-00C：持久备份与隔离恢复设计、差异表及离线验收

日期：2026-11-04。范围：**独立用量账本**，不是 G3 权威事件、relay 聊天库或完整业务灾备。

本轮结论：**设计及合成本地磁盘演练通过；真实持久备份、离机恢复与生产准入仍未验收。生产 HOLD。**
GM-00B 的现场内存 round-trip PASS 沿用交接证据，本轮没有重跑生产检查。

## 1. 已核对的基线

仓库 `joehyun06-ui/Tidal_Echo` 的 `feat/render-telegram-deployment` 在本轮读取时仍指向
`c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4`。以下审阅均固定该 commit。
这是**当前仓库分支核对**，不是本轮重新证明 Render 线上部署 SHA 或挂载健康。

| 文件 | Git blob SHA | 核对结果 |
|---|---|---|
| `backend/usage_ledger_backup.py` | `3b92d18f40a3b98a3da3353d20e418a24423b0b5` | 已有 SQLite Online Backup、目标完整性/外键检查、私有新文件及临时隔离恢复；CLI 仅 mock |
| `backend/usage_ledger_paths.py` | `be2e450a94262de526e1d640b8751b4a572fc906` | 主库及 SQLite sidecar 路径分离；现有检查不等于对恶意文件系统竞态的防护 |
| `backend/usage_ledger_storage.py` | `91f19285841c266cea85cb1bbeda729f39db68ed` | 有签名分页、revision、保留/删除语义；不是备份调度器 |
| `backend/usage_ledger_preflight.py` | `f045a5566773eec477a63e5e5de44f786c690bfd` | `review_plan` 是无凭据计划审查；声明通过不证明真实持久性 |
| `docs/usage-ledger-backup.md` | `1883775e8be31905008b23aaf5b532404904e608` | 原文已明确：无自动加密/离机/保留调度，旧快照不能直接接回生产 |
| `render.yaml` | `19ab578864778411b74d1293377f13e963708398` | 声明 `/var/data`、1 GiB、单实例、`autoDeploy=false`；不是外部备份目标或真实库存证明 |

配置计划实际在 `usage_ledger_preflight.py`，没有把猜测的 `usage_ledger_plan.py` 当成现有实现。
另已审阅 Ledger schema、CollectionJournal、报告、既有 backup/storage 测试。
`requirements.txt` 已固定 `cryptography==50.0.2`，本轮无新增依赖。

本轮查询 PR #199 仍为 **open / draft / merged=false**。未修改该 PR；交接中的历史 Actions
31/31 不在本报告中冒充本轮重跑结果。

## 2. 差异表

| 能力 | 基线状态 | 本轮补齐 / 剩余项 | 准入类别 |
|---|---|---|---|
| WAL 一致性快照 | 已有 Online Backup | 复用原实现，合成读事务保持 WAL，恢复包含已提交事务 | 合成已验证 |
| 单独落盘文件 | 已有 0600 私有文件、目标 fsync | 加密归档另做文件 fsync、独占发布与父目录 fsync | 合成已验证；断电未测 |
| 加密及来源完整性 | 原工具不提供 | AES-256-GCM；manifest 与 DB 一起加密认证；版本/key ID 绑定 AAD | 合成原型；真实密钥未配 |
| manifest | 原工具仅返回校验摘要 | DB SHA-256/长度、完整 schema hash、user_version、所有表计数及创建信息 | 合成已验证 |
| 完成标志 / 半成品 | 原工具不保证崩溃无残留 | `.partial-*` 不参与恢复；完整文件独占发布；同步失败不得回报成功 | 故障注入已验证 |
| 隔离磁盘恢复 | 已有临时目录恢复 | 先认证解密，再仅写新临时目录；校验时不调用会修改 schema/WAL 的 Ledger 构造器 | 合成已验证 |
| 版本兼容 | 只有必需表/列最小集 | archive v1 + 外部期望 schema 精确匹配；默认拒绝未知/旧 schema，不做隐式迁移 | 合成已验证 |
| 私有独立故障域 | 未建立验收证据 | 本轮模拟 vault 仍在同机临时目录，明确不算离机或介质隔离 | 待选目标、待授权 |
| 密钥备份 / 轮换 | 未建立本轮证据 | 原型只用临时随机合成密钥；没有真实密钥托管与回收 | 待设计、待授权 |
| 保留、频率、RPO/RTO | 未批准 | 仅给候选参数，不设生产计划 | 待用户决定 |
| 真实恢复 ACL | 未验收 | `authorized=True` 只是测试显式许可标志，**不是身份认证或 ACL 实现** | 需真实权限设计 |
| 删除后恢复 / 新鲜度 | 旧快照存在风险 | 明确证明旧快照仍可能含后来删除的合成行；无自动晋升与恢复采集 | 需独立新鲜度及撤销核对 |
| 跨库一致性 | 独立账本快照不提供 | 不复制 relay/native/G3；不宣称它们具有同一事务游标 | 仍未建立 |

## 3. 建议流程及证据边界

1. 经明确授权的维护入口只读打开**独立账本**，核对生产路径/模式/版本/容量；不接受 relay
   聊天库。复用 Online Backup，不直接复制正在运行的主 DB 文件，不 checkpoint 来源。
2. 在受控私有暂存区生成关闭的独立快照。只有新目标转换 DELETE journal 模式；核对必需
   schema、完整性、外键，记录该**快照**的全部表计数与哈希。不要与并发变化的实时库计数硬比。
3. 为快照构建 manifest，整体经 AES-256-GCM 加密。格式/版本及可信 key ID 进入 AAD。
   每份归档使用新 96-bit nonce；真实密钥的使用上限与轮换规则仍须运维设计。密钥不随归档放置，
   不进入仓库、日志或报告。manifest、cursor_key 等数据库内容均保持在密文内。
4. 本地原型写私有 partial、fsync 文件，再通过同文件系统的独占 link 发布最终文件、fsync
   父目录；不覆盖既有归档。发布后目录同步失败可能已留下完整最终文件，但**操作不得报成功或
   自动覆盖重试**。真实对象存储须另实现独占对象键、完整上传确认、重新下载验证和失败终态。
5. 经授权上传到独立私有故障域；最小权限分别限制写入、恢复读取和删除。必须重新取回已保存
   密文并验证，只有本地上传成功回执不足以证明可恢复。此步本轮未实现、未执行。
6. 恢复只去新建隔离目录/环境，先认证，再解析严格 manifest；比较独立受信的 schema 兼容清单，
   校验 DB 哈希、长度、完整性、外键、表计数及 user_version，最后核对签名分页、设置 revision、
   序号、退役与保留规则。生产 key ID/期望 schema 不得从未经认证的备份自我声明中取信。
7. 隔离验收后仍不接回线上。必须核对快照之后的删除、撤销、会话权威、运行时 HMAC 密钥与
   新通知基线；不能读取最新撤销记录时停止，不根据备份时间戳宣称“最新”。真实恢复与生产切换
   是两项单独授权，完整性通过不产生恢复生产的权限。

SQLite `mode=ro` + `query_only` 是逻辑只读。WAL 场景可能涉及 `-shm`/`-wal` sidecar，
不描述为文件系统绝对零写入。原型的 `immutable=1` **只用于已经关闭的独立快照或刚解密的新文件**，
不能拿它读取生产 WAL 主文件并绕过 WAL。

原型仅支持本轮验证的 Linux/POSIX 文件操作，使用单一所有者、可信临时目录；不是对恶意并发替换路径的加固实现。临时文件清理不保证
介质安全擦除，历史空闲页和旧归档可能含已逻辑删除的数据。真实生产需加密暂存、崩溃清理、
保留/删除传播、key escrow 与密钥轮换方案；没有这些条件不得拿原型直接制作生产备份。

本地 fsync + reopen 只验证本轮进程的磁盘 I/O 路径；没有做电源故障、跨进程重启或跨主机恢复。
AEAD 只能校验持密钥者提供的归档，不能建立独立可信 C、不可回滚下界或最新性。

## 4. 离线代码与可复现验收

新增且未接入运行时的文件：

- `backend/usage_ledger_archive.py`：只暴露 `--mock`；无生产路径、密钥、恢复目标、网络上传、
  环境变量或调度参数。私有辅助函数仅供合成测试，不是已获准的生产运维 API。
- `backend/tests/test_usage_ledger_archive.py`：31 项合成测试；不访问真实账户、数据库或模型。
- 本设计文档。现有 Python CI 使用 `test_*.py` 发现新测试，不修改部署或 CI 配置。

本轮本地环境：Python 3.12.14、SQLite 3.53.1、cryptography 50.0.2；不代表 Render 运行时版本。

```bash
python -m backend.usage_ledger_archive --mock
python -m unittest \
  backend.tests.test_usage_ledger.NormalizationTests \
  backend.tests.test_usage_ledger.StoreTests \
  backend.tests.test_usage_ledger_storage.StorageTests \
  backend.tests.test_usage_ledger_archive
```

**本地结果：73/73 PASS = 新增 31 + 既有账本/存储核心 42。** 原有 backup mock rehearsal
在新增测试中再次执行通过。两个新 Python 文件语法解析通过；基础依赖源码与固定 commit 的
Git blob SHA 已逐一核对。不是完整仓库测试通过声明。

曾尝试三个完整测试模块，共 75 项：72 PASS，3 项路由集成测试因本地缺少 `httpx` 和部分
路由/会话源码未能运行。随后新增 1 项序号验证并明确运行上述 73 项核心集合；未把那 3 项
环境缺项改成 skip 或通过。完整仓库路由/端到端回归结果以本轮 Draft PR 的 CI 为准。

| 测试组 | 拒绝 / 成功门槛 |
|---|---|
| WAL、磁盘重读、隔离恢复 | 已提交 WAL 行进入快照；合成源 main/WAL 字节不变；取回密文后恢复快照逐字节一致 |
| 权限及目标保护 | 文件 0600、目录 0700；无显式测试许可拒绝；已有归档不覆盖；符号链接/公开权限拒绝 |
| 加密与篡改 | 错 key/key ID、magic/nonce/ciphertext/tag 改动、截断/追加均拒绝；认证失败之前不写明文 |
| manifest / schema | 未知版本、布尔冒充版本/计数、字段缺失/增加、错误范围、DB hash/长度/计数不匹配均拒绝 |
| 数据库失败 | 坏文件、已认证但坏 b-tree、混合聊天表、外键错误、残留 sidecar 均拒绝 |
| 发布中断 | link 前失败不产出 final；旧 partial 不参与恢复；目录 fsync 失败不回报成功、不覆盖重试 |
| 行为与清理 | 游标、设置 revision、退役、原始用量过期、序号单调性保留；异常退出移除隔离副本 |
| 隔离边界 | 恢复校验不初始化 Ledger；CLI 不接受 source/target/restore/key；断网运行；错误和报告不泄漏行或密钥 |
| 旧快照 | 证明后来删除的行可仍在旧备份，因此成功解密不代表最新授权或允许重启采集 |

样例输出同时保留：`basis=synthetic_local_disk_only`、`offsite_verified=false`、
`power_loss_verified=false`、`production_backup_created=false`、
`production_restore_verified=false`、`enables_production=false`、`production=HOLD`。

## 5. 真实备份前待定条件与费用

以下是准备下一轮方案的选项，**没有设置或批准任何生产计划**。

| 用户需决定 / 提供 | 候选约束 | 本轮状态 |
|---|---|---|
| 私有备份目标 | 已有、经核实独立的私有存储，或新建私有对象存储；不能只换 `/var/data` 下一个目录 | 未选、未创建 |
| 读取内容和授权范围 | 仅独立用量账本；私有恢复人员/环境、允许的权限、数据驻留位置需明确 | 无生产数据访问/复制 |
| 真实密钥保管与应急取回 | 与生产磁盘分离；备份读取与密钥取回权限分别受控；演练丢失/轮换场景 | 未配真实密钥 |
| 频率与恢复目标 | 可讨论每日一次、RPO 24h、RTO 2h；这些是候选目标，须真实演练后才能宣称达到 | 当前 RPO/RTO 未建立 |
| 保留与删除传播 | 可讨论 7 日 + 4 周；须同时规定用户删除后的旧副本处理与销毁，不默认永久保留 | 未设保留计划 |
| 费用上限 | 选择目标后报价并明确月上限；没有批准购买、KMS、跨区流量或定时任务 | 本轮没有创建收费资源 |

粗略容量估算应取**实测加密归档大小 × 保留份数**；费用还包括请求、恢复取回、流量和密钥服务。
账本曾以 16 MiB 作为检查保护阈值，它不是实测归档大小，也不能据此预填具体供应商价格。
首版本地原型没有增加服务；不需要商业 C、Rekor 或 Object Lock 才能继续设计。

## 6. 三道独立验收门槛与下一断点

| 门槛 | 当前结论 | 下一次必须取得的证据 |
|---|---|---|
| A. 合成设计/本地磁盘演练 | **PASS，边界如上** | Draft PR 完整 CI；审查原型限制 |
| B. 真实持久备份可恢复 | **NOT RUN / BLOCKED** | 明确授权后的真实一致快照、独立私有持久目标、密钥应急取回、重新下载及隔离恢复、实际 RPO/RTO |
| C. 生产启用准入 | **HOLD** | 最新删除/撤销/会话权威核对、跨库边界、运行时通知连续性、回滚方案和单独生产批准 |

下一步先完成 B 的具体目标、权限、密钥与费用选择，然后形成可审阅的真实执行计划；此文没有
授权执行 B 或 C。不得把模拟 vault 当作持久远端备份，不提供可以直接粘到 Render Shell 的生产命令。

本轮未更改生产 DB、环境变量、Render/Vercel/Caddy、WAL 模式、G3 冻结账本；未合并 PR，
未新增外部模型 API 请求、自动记忆写入或生产备份计划。原 G3-QU 36/36、108/108 和历史 OpenAI
7/7 冻结；GM-01B 真实 G3 source/event/revision 接入仍待核实，商业 C 仍不进入日常链路。

## 7. 核对依据

- 固定源码：[backup](https://github.com/joehyun06-ui/Tidal_Echo/blob/c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4/backend/usage_ledger_backup.py)、[既有设计](https://github.com/joehyun06-ui/Tidal_Echo/blob/c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4/docs/usage-ledger-backup.md)、[Render 声明](https://github.com/joehyun06-ui/Tidal_Echo/blob/c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4/render.yaml)。
- 官方机制说明（本轮查阅）：[SQLite Online Backup](https://www.sqlite.org/backup.html)、[WAL 与只读条件](https://www.sqlite.org/wal.html)、[cryptography AEAD/AESGCM](https://cryptography.io/en/latest/hazmat/primitives/aead/)、[Render 持久磁盘](https://render.com/docs/disks)。官方平台能力不是本服务已执行或已验收的证据。
- 输入交接：`guiting_GM00B_to_GM00C_handoff_20261104(1).md`；总方案：`guiting-g3-ombre-kimi-execution-plan-v1.1-20261104(1).md`。
