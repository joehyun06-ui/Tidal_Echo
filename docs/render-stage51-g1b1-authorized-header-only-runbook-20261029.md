# 归汀 Stage51 — 经业主批准的 G1B-1 文件头安全核查工具与运行门禁

> 授权时间：2026-10-29。所有者批准 G1B-1 安全工具开发与后续文件头读取；G1B-2、G1C、G1D 未获授权。
> 当前：仅源码和合成验证。必须等最终精确提交 Linux/Windows CI 全绿并核对生产冻结状态才允许主人本机手动运行。PR 继续保持 Draft。

## 范围

- 固定 Linux `/var/data`，排除 Stage50 G1A 已知条目，恰好 11 个单硬链接普通 SQLite 扩展名文件与 4 个目录。禁止递归或顺带访问其他资产。
- Stage50 已审查的 `scan_once` 条件与匿名指纹在 G1B1 单文件脚本中保持一致；合成测试比较两版快照。
- 仅 `O_RDONLY|O_NOFOLLOW|O_NOATIME|O_CLOEXEC|O_NONBLOCK`、目录 fd 相对打开；目录本身也要求 `O_NOATIME`。权限不足、不支持或元数据漂移立即停止，不降级、不重试。
- 每个候选只执行一次 `pread(100, 0)`，只检查格式头。不得读表、Schema、SQL、WAL/SHM、密钥、记录、配置，不调用数据库引擎、外部网络或写文件。
- 仅输出 11 个文件头匿名分类汇总，不输出原始文件名、路径、字节、哈希或错误栈。文件头兼容不证明数据库真实有效或所属应用。
- G1A 名称分类 11/11 未分类、0 身份确认；本阶段即使文件头 11/11 通过，仍保持身份确认 0、SQLite 有效性 UNKNOWN_NOT_VERIFIED。

## 离线验收

- 以完全虚构的临时文件在 GitHub Actions Linux、Windows 测试：11+4 正常、头格式、截短、伪造、页大小、版本、符号链接、硬链接（POSIX）、漂移、权限失败、原始数据不外泄。
- Linux 用临时虚构文件验证生产代码路径 `O_NOATIME` 与每文件唯一 `pread(100, 0)`；不得把它当成生产扫描。
- 全部专项测试通过后核实最终提交 SHA、源代码、生产冻结状态和主人既有严格 SSH 主机键配置。若有失败或 SHA 变化，暂停。

## 手动运行门禁（仅在专项 CI 全绿后）

1. Windows PowerShell 从仓库已审定的**完整提交 SHA**创建新的、独立且不存在的审计 worktree，并核对 `git rev-parse HEAD` 精确匹配。不要用随时间变化的 branch HEAD。
2. 审计源码位置：`scripts/render_stage51_g1b1_header_readonly.py`。仅将这份源代码通过既有、已验证主机键的 Render SSH 连接标准输入喂给 `python - --perform-g1b1-header-only --owner-authorized-g1b1`。不在远端写入脚本文件。
3. 本机不得同时使用任何包含真实数据的 `--synthetic-root`；该选项仅用于虚构 CI，且需要 `GUITING_STAGE51_SYNTHETIC_ONLY=true`。
4. 只分享最后的匿名 `stage=51` JSON。出现 `STOP_SCOPE_CHANGED_OR_UNAVAILABLE` 或 `STOP_READ_NOT_SAFE` 必须立即停止，不得自动重试。
5. Render 连接器没有通用 SSH 执行/文件读取入口，不能因为用户给了读取授权就声称已执行生产检查。

## 匿名结果语义

- 成功返回 `stage=51`、`gate=G1B1`、`result=HEADER_CLASSIFICATION_ONLY`、`candidate_count=11`，以及 `header_compatible_count`、`header_nonmatching_count`、`header_insufficient_or_unsupported_count`。三者之和必须严格为 11。
- `identity_confirmed_count=0`、`valid_sqlite_count=UNKNOWN_NOT_VERIFIED`、`schema_checked=false`、`database_engine_opened=false`、`source_writes=false`、`real_files_exported=false`、`backup_or_restore=NOT_AUTHORIZED_NOT_TESTED`。
- 成功只表示部分格式指标，不能提高 G1C/G1D 的备份恢复验收等级。v48 36/36、108/108 冻结不变。

参考：SQLite https://www.sqlite.org/fileformat.html 、WAL https://www.sqlite.org/wal.html。
