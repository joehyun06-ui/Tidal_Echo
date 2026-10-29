# 归汀 Stage53-P0C — 已授权的 CAP_FOWNER 只读生产查询运行说明

> 2026-10-29 所有者明确单独批准 Stage53-P0C 真实查询。本批准**只包含当前诊断 Python 线程的 effective CAP_FOWNER**，不包含运行 G1B-1、P1、G1B-2、G1C、G1D 或变更任何权限。GitHub Draft PR 不会因授权自动部署。

## 已有事实和诊断意义

- Stage51 owner-reported `STOP_READ_NOT_SAFE` 未提供具体 syscall/errno 或证明零字节读取。Stage52-P0 owner-reported `root_owner_relation=MISMATCH` 只表示当时运行 Python 的有效 UID 与 `/var/data` 目录所有者不同。
- Linux `open(2)` O_NOATIME 需要有效 UID 与目标对象所有者一致，或具有 CAP_FOWNER 且 UID 映射满足条件。`MISMATCH` + 当前 `NO` 会增加**根目录 O_NOATIME 拒绝**的解释力度，但不能证明此前 G1B-1 的具体失败；当前 `YES` 也不能证明旧进程的权限、namespace 映射、DAC/MAC 或文件系统兼容性。
- 本查询不访问 `/var/data`；只通过 Linux capget(2) 对当前线程读取 effective CAP_FOWNER 位，不查询其他进程，也不输出 capability bitmask 或任何身份值。

## 执行工具和精确范围

源文件：`scripts/render_stage53_p0c_cap_fowner_readonly.py`。

- 强制 Linux x86_64；reviewed `SYS_capget=125`, capability API version 3 (`0x20080522`)，两字的 cap data 结构，header pid=0 表示当前线程。架构不支持时立刻固定 STOP，不使用 `/proc`、libcap、外部命令或架构猜测回退。
- 只用 `ctypes.CDLL(None).syscall` 发起一次只读 `capget`；不调用 capset/prctl/chmod/chown/sudo，不创建文件、不打开、枚举或检查根目录、候选文件、schema、WAL、密钥、配置，不网络传输。
- 只有匿名 JSON 包含 `cap_fowner_effective=YES|NO`；失败固定 `STOP_CAPABILITY_CHECK_UNAVAILABLE`，不会输出 UID、GID、其他 capability、mask、errno、系统路径或 traceback。
- 两个生产开关必须同时存在：`--perform-stage53-p0c --owner-authorized-stage53-p0c`。`--synthetic-effective-mask` 为受环境门禁保护的纯虚构测试入口，生产时**禁止传入**。

## 运行门禁

1. GitHub 上精确最终 SHA 的 Linux、Windows 专项虚构 capability 测试均成功；源码、完整 SHA 和脚本路径已检查。该 CI **不得**调用真实 capget，也不访问 Render。
2. Render 控制平面只读核对生产部署仍为审核基线，自动部署关闭，不改变服务和磁盘。
3. 用户在 Windows 自己的仓库中 `git fetch` 精确 SHA，用新的独立 worktree 固定版本，核对 `git rev-parse HEAD` 与完整 SHA 一致，然后通过既有严格验证主机密钥的 SSH 通道以 stdin 执行该单文件代码。
4. 一次成功只返回匿名 Stage53-P0C JSON。任意 STOP 立即停止，不允许重复尝试或改权限。**不要发私钥口令、原始 SSH 日志、capability 掩码、UID/GID。**
5. 用户结果只用于决定下一轮要不要做代码和权限设计复核；不自动解锁任何数据库操作。

## 成功/失败结果

成功：`stage=53`, `gate=G1B1_PERMISSION_P0C`, `scope=AUTHORIZED_CURRENT_PROCESS_CAPABILITY_ONLY`, `result=CAP_FOWNER_EFFECTIVE_HINT_ONLY`, `cap_fowner_effective=YES|NO`, `filesystem_objects_inspected=0`, `directory_opens=0`, `candidate_file_opens=0`, `candidate_content_bytes_read=0`, `g1b1_retried=false`, `source_writes=false`。

失败：`{"stage":53,"gate":"G1B1_PERMISSION_P0C","result":"STOP_CAPABILITY_CHECK_UNAVAILABLE"}`。

## 不变的边界

Stage50–53 相关审计 PR 保持 Draft、未合并、不部署。G1B-1 继续停止；P1/G1B-2/G1C/G1D 锁定；v48 正式 36/36、108/108 验收门槛不变。Linux 内核参考：https://man7.org/linux/man-pages/man2/capget.2.html 和 https://man7.org/linux/man-pages/man2/open.2.html。
