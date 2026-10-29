# 归汀 Stage53 — 根目录 O_NOATIME / CAP_FOWNER 权限资格诊断（仅设计）

> 2026-10-29 — **DESIGN_ONLY / NOT_AUTHORIZED_FOR_PRODUCTION / NOT_EXECUTED**。
> 本 PR 只增加安全诊断设计文档。未新增可执行生产工具、SSH 命令、生产数据、密钥或任何 Render 操作。
> 原 Stage52 的授权及报告不自动授权对进程能力集合做新一轮生产采集；执行 Stage53-P0C 需要明确的独立授权及版本锁定。

## 1. 已确认的事实与未证明的因果

- Stage51 G1B-1：所有者使用精确源 SHA `3a52285aa6d12b9b43a05b95af0b5c37ff4573ff` 执行一次受限检查，报告 `STOP_READ_NOT_SAFE`。该统一停止码合并了多种可能的失败；未知具体系统调用、errno、部分文件头是否已读取。
- Stage52 P0：所有者使用精确源 SHA `692a2362098ebf001aa178f8d5a646f35acf02f6` 返回 `ROOT_OWNER_RELATION_HINT_ONLY`，且 `root_owner_relation=MISMATCH`；根目录没有被打开或枚举，候选文件没有被打开，字节读取数为 0。此结果是所有者提供的匿名脚本输出，不是独立的文件系统日志。
- G1B-1 源码的第一个生产对象打开动作是固定 `/var/data` 根目录，使用 O_RDONLY、O_DIRECTORY、O_NOFOLLOW、O_CLOEXEC、O_NOATIME。根目录打开失败进入 `STOP_READ_NOT_SAFE`；后续候选文件 open/read 的失败也进入同一个 STOP。
- Linux open(2)：使用 O_NOATIME 要求有效 UID 与被打开对象所有者 UID 相同，或者进程在适用用户命名空间具有 CAP_FOWNER 并满足所有者 UID 映射条件；部分文件系统不能保证 O_NOATIME 有效。
- 所有者关系 MISMATCH 加上**仍未检查的** CAP_FOWNER 资格是一个解释 G1B-1 停止的有力假设；但不能写成‘已经确认 root open 报 EPERM’。前后 SSH 进程和能力状态也可能不同。

## 2. Stage53-P0C：提议的最小独立诊断

**核心问题：** 当前 SSH 启动的审计 Python 进程，在其有效 capability set 中是否具有 CAP_FOWNER？

### 2.1 首选技术路径（仅设计）

- 只在 Linux 使用只读的 capget(2) 系统调用检查**调用线程自身的 effective CAP_FOWNER 位**。工具只能做能力查询，不允许使用 capset(2)、prctl 修改权限、提权或任何外部命令。
- 优先由经过审核的系统调用包装器返回只读能力集合，并在进程内只取 CAP_FOWNER 的有效位。明确限定 Linux API capability version，失败或不支持一律返回 UNKNOWN；不允许读取 /proc/self/status、/proc 其他路径、环境变量、文件系统挂载数据或 UID 映射文件。
- 不主动访问 /var/data：不执行 lstat、stat、open、scandir、listdir，不尝试打开目录或候选，不读取任何数据库文件头或 Schema，不更新 atime。
- 不传输 capability 掩码、其他能力名称、内核版本、实际 UID、GID、namespace ID、原始 errno、调用栈和用户资料；输出仅 YES/NO/UNKNOWN 三类汇总。
- 禁止通过 capget 是否成功或 CAP_FOWNER 结果自动执行任何其他动作。不存在 P0C 到 P1、G1B1 的自动链式执行。

### 2.2 仅能陈述的诊断语义

| P0 根所有权 | P0C 有效 CAP_FOWNER | 允许的推论 | 禁止的推论 |
| --- | --- | --- | --- |
| MISMATCH | NO | 在条件不变、没有其他授权规则时，不满足 Linux O_NOATIME 根打开的两种资格条件；可将根目录 NOATIME 拒绝列为高度可信的停止解释 | 不能声称已捕获原来 syscall/errno，不能断言任何候选损坏 |
| MISMATCH | YES | 能力位存在；是否满足用户命名空间映射、DAC、MAC、文件系统与实际 I/O 仍未知 | 不能认为重新执行 G1B1 已安全 |
| MISMATCH | UNKNOWN | 无法进一步缩小原因 | 不得推测 YES 或 NO |
| MATCH | 任意 | 与此前用户报告的 P0 状态不一致或环境可能变化；必须停止并重新审查证据 | 不能自动重扫或读取其他文件 |

以上比较仅可将此前的 P0 结果作为**用户报告的历史线索**，不能伪装成同一瞬间的同步核查。任何 P0C 结果仅说明当前诊断进程的能力，不证明旧 G1B1 Python 进程具有相同能力。

## 3. 未来匿名输出合同（占位，不是生产证据）

成功时仅返回以下字段，不包含权限集合和原始值：

```json
{
  "stage": 53,
  "gate": "G1B1_PERMISSION_P0C",
  "scope": "AUTHORIZED_CURRENT_PROCESS_CAPABILITY_ONLY",
  "result": "CAP_FOWNER_EFFECTIVE_HINT_ONLY",
  "cap_fowner_effective": "YES_OR_NO",
  "filesystem_objects_inspected": 0,
  "directory_opens": 0,
  "candidate_file_opens": 0,
  "candidate_content_bytes_read": 0,
  "g1b1_retried": false,
  "source_writes": false
}
```

实际值必须仅为 YES 或 NO；如果 capget 无法安全完成，必须报告固定 STOP_CAPABILITY_CHECK_UNAVAILABLE，而不是虚构 UNKNOWN/NO。成功也不报告推断的 errno 或‘已证明旧失败原因’。

## 4. 纯合成安全验证清单（尚未编写或运行）

- 独立审计 Draft 开发，不将诊断功能写进原 Stage51 G1B-1 或 Stage52 P0 的生产脚本；保持提交精确锁定。
- Windows/Linux 合成测试必须使用假 capability bitmask/mock 系统调用模拟：有效位 YES、NO、未知版本、调用失败、mask 长度异常、输出不含掩码或其他能力信息。
- Linux 代码结构审计：只有查询操作；没有 capset/prctl/sudo/chmod/chown，没有 /proc 和环境变量、网络、os.open、lstat、stat、目录枚举、sqlite3、pread、写入。
- 负面测试通过异常信息注入秘密路径、UID、capability 数字和 errno，保证只输出固定脱敏停止码。
- 精确源码与专项 GitHub CI 全绿之后，仍须审查数据流、工具注入机制和 Render 环境适配性；没有额外授权就不得在真实 SSH 中调用。

## 5. 决策与冻结

- 当前授权边界仅支持**代码/方案设计**，不在 Render 上执行 CAP_FOWNER 查询、不上传权限集合、不再运行 G1B-1。
- 新的 P0C 诊断若获授权，也只是**当前进程能力位**检查；若结论符合 root NOATIME 缺少资格的假设，优先记录为根因线索并停止，不以修改 chown/chmod 或移除 O_NOATIME 绕过。
- P1 11 个候选所有权统计、G1B-1 再执行、G1B-2 Schema、G1C 备份与 G1D 恢复继续分别锁定。
- 归汀正式 v48 36/36、108/108 验收门槛不变；全部相关审计 PR 保持 Draft、未合并、未部署。

## 6. 参考

- Linux open(2) O_NOATIME: https://man7.org/linux/man-pages/man2/open.2.html
- Linux capabilities(7): https://man7.org/linux/man-pages/man7/capabilities.7.html
- P0 源码审计：PR #191；G1B-1 停止归档：PR #189；Stage52 设计：PR #190。
