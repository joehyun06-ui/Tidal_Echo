# 归汀 Stage51 — G1B 私有 SQLite 身份核查授权方案（设计稿，未授权）

> 状态：DESIGN_ONLY / NOT_AUTHORIZED / NOT_EXECUTED；2026-10-29。
> 本文提交至独立 Draft PR，目的仅为评审边界。它不是生产读取许可，也不是执行脚本、审批表、备份指令或恢复指令。

## 1. 已有依据与不能推导的结论

- Stage50 G1A 用户返回脱敏 JSON：/var/data 根目录内，排除原有固定清单后，观测到 11 个 .db/.sqlite/.sqlite3 扩展名候选和 4 个未分类目录；11 个候选名称线索全部落入 unclassified_name_hint。
- G1A 输出 NAME_HINTS_ONLY、identity_confirmed_count=0、valid_sqlite_count=UNKNOWN_NOT_INSPECTED。该输出由用户提供，与既定 G1A 代码结构相符，但不是独立服务器遥测证明。
- G1A 对应 Draft PR #187，最终审计代码版本 8bb7355ceabe02a9bcdd1ff115d686b6fd995def。精确 SHA 的 32/32 PR CI 为 Success；其中 G1A Linux/Windows 合成测试各 7/7。合成测试不能证实真实数据身份。
- 当前生产冻结基线此前记录为 49ebe9a0e7f391efda86585b2bbe8f1bcff4288f、Render 自动部署 OFF；本方案未重新验证任何 Render 生产状态。
- 名称、扩展名和文件头均不能证明生成者、用途、数据完整性、可恢复性或账本激活状态；正式 v48 36/36、108/108 验收门槛不变。

## 2. G1B 拆分为两个不继承权限的独立门禁

| 子门禁 | 预期动作 | 此时权限 |
| --- | --- | --- |
| G1B-1 | 最小 SQLite 文件头分类（每个候选最多前 100 字节） | 只批准设计，不授权真实读取 |
| G1B-2 | 私有 Schema/对象结构读取，用于进一步身份核查 | 暂不授权，另行做风险评审、选择方法并申请许可 |
| G1C | 一致性备份、WAL 处理、快照或导出 | 未授权 |
| G1D | 隔离恢复及恢复测试 | 未授权 |

**阶段推进原则：** B1 的成功、文件头匹配、PR 合并、CI 全绿都不能自动开启 B2/C/D。每一步必须再由所有者明确限定范围、方法和可输出字段。不得把“继续方案”解读为对 Render 的执行授权。

## 3. G1B-1：仅文件头核查的拟议执行合同（尚未实现）

### 3.1 候选集与入口

- 入口仅为所有者将来手动授权、手动触发的固定 POSIX /var/data；不在 CI、GitHub、自动部署或外部主机访问真实生产磁盘。
- 候选恰好为 Stage47/Stage50 定义的 11 个根目录非固定清单的普通、单硬链接 SQLite 扩展名文件。4 个未分类目录不递归；固定清单、符号链接、其他扩展名、WAL/SHM、Codex Home、Usage Ledger 配置、密钥和任何其他资产都不属于范围。
- 调用前必须复用经验证的 G1A 范围识别规则与根目录 fd 相对元数据检查。若数量、类型、链接、候选身份或扫描快照漂移，统一 fail closed；不自动扩大范围、不隐式重试、不做“按相似名称推断”的额外扫描。

### 3.2 读取上限与低影响访问

- 在 Linux/POSIX 上从固定目录描述符相对打开候选：只读、禁止跟随符号链接、关闭继承，尽可能加 no-atime 且必须对潜在 atime 更新给出 fail-closed 证明/控制；不允许在 no-atime 受限时静默降级为可能修改访问时间的读取。若无法保证既定零源修改要求，则停止并重新请示，不擅自创建副本。
- 打开前后用 fstat/stat 比对 dev、inode、普通文件、链接数、文件大小、修改时间等匿名内存元数据；使用有严格上界的定位读取（例如 pread），**每个被核查候选最多读取前 100 字节**，整个运行最大候选数 11；不遍历后续页面、不读取记录、表、密钥或正文。
- 任何 TOCTOU 风险、内存/文件读取故障、元数据不一致、未知平台/文件系统行为、权限缺失或文件访问产生潜在写入时，立刻停止且不输出部分统计。二次快照不是文件系统事务一致性的证明。
- 不使用 sqlite3.connect、SQLite CLI、PRAGMA、ATTACH、VACUUM、备份 API、PRAGMA integrity_check 或任何数据库引擎；不打开 WAL/SHM。不开网络、不调用模型、不创建暂存文件、不输出文件名、日志路径、文件内容或哈希。

### 3.3 文件头分类与严格语义

- SQLite 官方规范：数据库文件头为前 100 字节；字节 0–15 为 SQLite format 3\0，页大小在偏移 16–17，读写版本在 18–19。B1 在内存中检查固定标识、可解释的页大小和基础格式字段，得到“文件头与 SQLite 格式兼容”的提示，而**不是**整个数据库有效性。
- 对不足 100 字节、标识不匹配、版本不支持、格式字段异常按匿名分类累计；不要返回单文件结果或文件顺序，也不要打印实际前 100 字节、应用 ID、user_version、版本戳或其他原始元数据。
- 即使全部 11 个文件都通过头检查，identity_confirmed_count 必须保持 0、valid_sqlite_count=UNKNOWN_NOT_VERIFIED；不能据此证明 Codex 任务、Ledger、历史归档或恢复资格。

### 3.4 仅可公开的统计字段（拟议，不是当前执行结果）

成功格式示意（数字为占位，不代表任何生产观测）：

```json
{
  "stage": 51,
  "gate": "G1B1",
  "scope": "AUTHORIZED_HEADER_ONLY_IF_LATER_APPROVED",
  "result": "HEADER_CLASSIFICATION_ONLY",
  "candidate_count": 11,
  "header_compatible_count": null,
  "header_nonmatching_count": null,
  "header_insufficient_or_unsupported_count": null,
  "identity_confirmed_count": 0,
  "valid_sqlite_count": "UNKNOWN_NOT_VERIFIED",
  "schema_checked": false,
  "database_engine_opened": false,
  "raw_names_or_header_bytes_emitted": false,
  "backup_or_restore": "NOT_AUTHORIZED_NOT_TESTED"
}
```

其中 null 必须在将来真正获授权执行之后才被实际聚合计数替换；正常成功时，三个头分类数之和必须严格等于 11。不得发布与文件名桶交叉关联的表格，以免重识别。失败仅输出固定 STOP_SCOPE_CHANGED_OR_UNAVAILABLE/STOP_READ_NOT_SAFE，不给出单文件错误、文件名、大小或堆栈。

## 4. G1B-2：Schema 审查暂时冻结

- 读取 SQLite Schema 会涉及数据库页及可能包含对象名、SQL DDL 和其他潜在敏感文本，属于明显超出 G1A/B1 的内容读取。不得因为 B1 header_compatible_count > 0 而执行。
- 活跃数据库可能使用 WAL；只读 SQLite 连接在某些条件下仍可能创建 -shm/-wal 辅助文件。SQLite immutable=1 会跳过锁和变化检测，不应作为对仍可能变化的生产数据库的简单安全替代。未经额外设计和授权，禁止生产 sqlite3.connect、mode=ro/immutable URI、PRAGMA、sqlite_master 查询。
- 如后续确实需要 Schema，请先单独审查是否可在获得许可的稳定、隔离材料上操作；任何副本创建、WAL 合并、在线备份或导出仍受 G1C 独立授权约束，不可借 B2 绕过。优先定义最小化结构指纹的本地计算规则、敏感对象名不回传，以及针对数据库兼容性与一致性的停止条件。
- 在 B2 未获单独授权之前，schema_checked=false、usage_ledger_activation=UNKNOWN_NOT_INSPECTED、database_identity=NOT_CONFIRMED。

## 5. 提交执行工具前必须通过的离线安全审查（本 PR 不包含实现）

1. **源代码与权限审查：** 审查打开参数、无写调用、no-atime 行为、文件描述符关闭、fd 相对路径、软硬链接防护、TOCTOU 对比、只读取上限和错误信息匿名化。
2. **纯合成 Windows/Linux 测试：** 正确 11 文件 + 4 目录、合法头、伪造头、截短头、不支持版本、错误页大小、目录数量漂移、符号链接、硬链接、类型变化、读前读后漂移、打开失败、模拟 atime 控制失败、重复执行、统一失败输出；Windows 只能走 synthetic 分支，实际生产 POSIX 门禁不可跳过。
3. **隐私负面测试：** 证明日志、异常、CI artifacts、PR diff、标准输出均没有原始名、路径、文件头原始字节、大小、时间戳、哈希、SQLite 原始字段或密钥。禁用网络、数据库引擎及任何持久化输出。
4. **运行隔离：** 不在 Render 服务启动脚本、生产发布流程或 GitHub CI 中挂载真实数据；只有后续明确授权与测试通过，才允许编制本机 SSH 人工触发步骤。
5. **版本锁定与运行审批：** 产生审定 SHA，交付离线合成证据，审查后再次向所有者取得精确 G1B-1 许可。未获得该许可，绝不在生产运行。

## 6. 所有者后续可单独批准的最小授权文本（现在未授予）

> 我单独授权 G1B-1，仅允许在 Render 既有 /var/data 根目录对 Stage50 的 11 个候选普通文件进行不超过每文件前 100 字节的只读 SQLite 文件头检查；不得读取 Schema、正文、记录、密钥或任何其他文件，不得修改源文件或 atime，不得备份、导出、迁移、恢复或部署。仅可返回匿名总数及格式分类统计。脚本需先通过独立 Linux/Windows 纯合成安全测试，并由我手动执行；若不能保证范围、安全性和零源修改则必须停止。此授权不包含 G1B-2/G1C/G1D。

该段仅为供将来审查的**示例措辞**，不是当前用户已发出的授权，也不会让设计 PR 自动获得访问权。

## 7. 当前结论与不可变门禁

- 本 PR 只能放入此设计文档；**不新增生产扫描代码、密钥、日志、数据库文件、真实测试数据或 SSH 执行命令**。保持 Draft，不合并、不部署。
- G1A 已完成匿名名称检查，未确认身份；G1B-1 尚未授权也未执行；G1B-2、G1C、G1D 继续冻结。
- Stage50 PR #187 和其代码、测试结果不因本提案改变；生产 LIVE/自动部署状态本 PR 不进行外部修改。
- 参考依据：SQLite 文件格式 https://www.sqlite.org/fileformat.html；SQLite WAL https://www.sqlite.org/wal.html；SQLite URI/immutable https://www.sqlite.org/uri.html。
