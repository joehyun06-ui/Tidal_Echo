# 归汀 Stage50 — 经业主单独授权的 G1A 私有候选身份核查（仅只读、匿名）

## 授权与来源

业主在 2026-10-29 单独明确：**“授权仅对 11 个候选文件进行私有身份核查。”** 授权限 G1A（候选身份和文件名线索），**不授权** G1B 读取文件头/SQLite schema/PRAGMA、不授权 G1C 在线一致性备份、不授权 G1D 隔离恢复；同样不授权读取 Codex Home、Usage Ledger plan/密钥、四个未分类目录正文或其他文件。

Stage47 由业主在 Render SSH 上观察到 `/var/data` 根目录存在 15 个**非固定清单**条目：11 个有 `.db/.sqlite/.sqlite3` 扩展名的普通文件候选，另有 4 个目录；当时没有其他未知类型。扫描只是一个时间点的元数据快照，不能证明这些扩展名候选是有效 SQLite 数据库；已知固定 `codex-generation.db` 不是那 11 个候选中的一个。

生产基线已只读核实：`ouou-home-telegram` LIVE `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`，autoDeploy OFF，原持久磁盘 `/var/data`。本阶段不修改生产服务/磁盘，不部署/合并，不读取任何生产文件正文和密钥，不传输真实 P1 文件。

## G1A 的“身份”和“提示”必须区分

仅观察文件名就可以归类出 `codex_name_hint`、`ledger_name_hint`、`archive_name_hint`、`unclassified_name_hint` 四种**名称启发式提示**。这些词只是**可能用途**，不是“确认是 Codex 任务数据库/Usage Ledger/备份文件”的证据。任何文件名都可能误导，尚无文件头、Schema 或生成者证明。

本轮自动化工具 **G1A 名称线索筛查**永远输出 `identity_confirmed_count=0`、`identity_confirmation=NOT_PROVEN_BY_NAME`、`valid_sqlite_count=UNKNOWN_NOT_INSPECTED`，不会将 11 个候选误认作 11 个有效数据库。若确实需要更强的私有身份确认，可在**用户自有、受控的 SSH 交互界面**中人工核对必要文件名与源码用途，但不能将明细回传给聊天/GitHub/CI。进入 G1B（头/Schema）必须另获批准。

## 已实现的只读工具

脚本 `scripts/render_stage50_g1a_private_identity_readonly.py`：

- 需要明确 `--perform-g1a-readonly`。生产根只能是 POSIX `/var/data`，打开目录使用 `os.O_DIRECTORY | os.O_NOFOLLOW`，候选使用目录 fd 相对 `os.stat(..., follow_symlinks=False)`，**不打开候选文件**，不跟随软链接，不递归扫描四个目录。
- 固定排除 Stage47 的 9 个已知根目录名称（含 `relay.db`、`codex-generation.db`），避免把已经识别的固定资产算进 11 个匿名候选。必须**恰好 11 个单链接普通 SQLite 扩展名文件、4 个普通目录、没有其他未知根条目**；类型、数量、访问失败或文件名漂移均中断并仅显示统一 `STOP_SCOPE_CHANGED_OR_UNAVAILABLE`。
- 重复两次只读根目录扫描，并在进程**易失性内存**比较文件名/设备/ inode/ 类型/大小/时间点元数据，发现两次扫描不一致则拒绝输出。两次相等仍是点状核对，不是并发写入锁。
- 只汇总 4 个名称提示桶的数量，**不显示**具体文件名、完整路径、Hash、单个文件大小、修改时间、inode、SQL 语句、表名、聊天内容或密钥；不把这些写入持久文件。任何诊断错误统一输出固定脱敏代码，无回溯/用户私有路径。
- 无 `sqlite3`、文件读取或写入、HTTP、SCP、外部子进程、模型调用。未触碰 P1 数据内容、不新建磁盘副本、不触发 Render 原生快照 Restore。
- `--synthetic-root` 仅需 `GUITING_STAGE50_SYNTHETIC_ONLY=true` 方可执行，是供 Windows/Linux CI 使用的**独立虚构测试根**，并不允许绕过真实生产 `/var/data` 固定路径要求。

### 仅在专项 CI 成功后，由所有者手动运行

Windows PowerShell 中：先创建一个新的独立审计工作树，不要覆盖以前目录；在现有本地 Git 目录基础上检查 commit 和脚本是否为此 PR 的**最终审定 SHA**。然后：

```powershell
Get-Content -LiteralPath "$audit\scripts\render_stage50_g1a_private_identity_readonly.py" -Raw |
  ssh -i "$HOME\.ssh\render_ouou_ed25519" `
    -o IdentitiesOnly=yes `
    -o StrictHostKeyChecking=yes `
    -o HostKeyAlgorithms=ssh-ed25519 `
    -o UpdateHostKeys=no `
    -T srv-d9dk0gbrjlhs73apgufg@ssh.oregon.render.com `
    'python - --perform-g1a-readonly'
```

上面的 SSH 指令只是代码经加密连接进入既有容器并由 Python 标准输入执行；不会在 Render 创建脚本文件，也没有打开被核查的候选文件正文。不要把实际文件名或私人 SSH 日志上传聊天；只把最末 `stage=50` 匿名 JSON 分享。

## 判断与后续授权

- 只允许将**经业主运行后的 `NAME_HINTS_ONLY`** 记录为 G1A 自动匿名名称线索核查完成；`identity_confirmed_count=0` 并不表示没有真正的数据库，而是没有实际证据确认文件身份。
- 若 `STOP_SCOPE_CHANGED_OR_UNAVAILABLE`，即使因新增/删除条目、link 或扫描竞争导致，也停止，不重试、不自动扩大范围。
- G1B 需要单独批准只读文件头/schema；G1C 需要单独批准特定文件的 SQLite 在线一致性备份；G1D 需要单独批准隔离恢复。不得因 G1A 结果和 P0 先前授权而跳过。
- Github Actions 只运行虚构文件树的合成用例，**不得**当作真实文件身份扫描结果。真实数据内容、密钥、配置、备份与恢复均不接触。
- v48 正式 36/36、108/108 冻结门槛保持不变。Stage50 审计 Draft 留在独立分支，禁止合并与部署。
