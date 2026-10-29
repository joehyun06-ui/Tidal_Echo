# 归汀 Stage52 — G1B-1 匿名权限诊断方案（仅设计）

状态：DESIGN_ONLY / NOT_AUTHORIZED_FOR_PRODUCTION / NOT_EXECUTED（2026-10-29）。
本次只批准继续设计；不等于批准对生产磁盘进行新一轮诊断、重新执行 G1B-1 或变更任何权限。

## 1. 证据与问题

- Stage50 G1A：用户返回 11 个 SQLite 扩展名候选、4 个目录，11 个名称提示均未分类，身份确认数为零。
- Stage51 G1B-1：用户在个人 SSH 中执行精确提交 3a52285aa6d12b9b43a05b95af0b5c37ff4573ff，收到脱敏结果 {"stage":51,"gate":"G1B1","result":"STOP_READ_NOT_SAFE"}。
- 此 STOP 是运行状态，不是数据库损坏证据，也不能证明没有读取任何文件头字节。G1B-1 当前保持停止，禁止再次运行。
- 代码审阅显示同一个 STOP 可能由 Linux 所需功能缺失、打开根目录的 O_NOATIME 失败、打开候选文件的 O_NOATIME 失败、文件 stat 或预设 100 字节读取失败触发。JSON 没有区分它们。
- Linux open(2) 对 O_NOATIME 的规则：需要进程有效 UID 与文件所有者一致，或具备适用的 CAP_FOWNER；某些文件系统可能不能完全保证不改变 atime。参考：https://man7.org/linux/man-pages/man2/open.2.html 。所有权不匹配属于假设，未被这次输出证明。

## 2. 新方案目标与绝对禁止

目标：未来经过单独授权后，只用匿名元数据关系判断当前 SSH 运行身份与 /var/data 根目录、11 个候选文件的所有者是否一致，并判断能否完成符合零源修改条件的目录枚举。所有结果仅是诊断线索，而非文件可读性证明。

禁止：
- 不读取任何候选文件头、Schema、表、正文、WAL/SHM、密钥、配置、历史日志及四个未知目录中的内容。
- 不打开候选文件（包括 O_PATH 或只读打开）、不执行 pread、SQLite 引擎或探测性内容读取；不打开其他固定资产。
- 不运行 sudo、su、chmod、chown、setfacl、setcap、权限提升或绕过，不更改挂载/atime，也不复制、备份、恢复或传输文件。
- 不使用会输出原始名称/UID/GID/权限位的 ls -l、stat -c、strace 等命令；不读取 /proc、自身环境变量或范围外资料；不回传任何原始错误、路径、inode、权限位和单文件判断。
- 不把现有 G1B-1 授权解释为 Stage52 的实际执行授权。

## 3. P0：根目录元数据关系（未来独立授权）

- 平台限制 Linux + 固定 /var/data。以 lstat/stat(follow_symlinks=False) 仅观察根目录自身元数据，并获取进程 geteuid；这一步不能枚举目录或打开任何候选文件。
- 检查目标必须为目录且不是符号链接。只在本进程内比较有效 UID 是否与根目录 st_uid 匹配，得到 MATCH/MISMATCH/UNKNOWN；不输出数值或路径。
- MISMATCH 不能证明 O_NOATIME 必然失败，因为 CAP_FOWNER 尚未获授权检查；MATCH 也不能证明后续读取一定成功。失败只返回固定 STOP_SCOPE_OR_ACCESS_UNVERIFIED。
- P0 不知道 11 个候选的当前情况；不得伪造 candidate_count=11 已检查的结论。

## 4. P1：根目录安全枚举与候选所有权匿名汇总（需要再次独立授权）

- 先证明相关文件系统及运行时可以满足对目录枚举的 atime 安全要求；如果不能证明，则停止，不把 O_NOATIME 当成对所有文件系统的绝对保证。
- 只对固定根目录执行一次 O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC|O_NOATIME 的安全打开，不降级、不重试；打开失败仅回传 STOP_ROOT_SAFE_ENUM_UNAVAILABLE。
- 在根 fd 上沿用 G1A 的范围保护和双次元数据快照：严格排除固定已知项目、只允许恰好 11 个单硬链接普通 SQLite 扩展名候选及 4 个其他普通目录；发现 symlink、特殊类型、新增/删除、数量漂移或元数据变化时丢弃中间结果。
- 仅对候选使用 dirfd 相对 stat(follow_symlinks=False)；不打开候选本身、不递归 4 个目录。只在进程内比较候选 st_uid 与 geteuid，累计匿名 owner_match_count / owner_mismatch_count / owner_relation_unknown_count，总和必须恰好 11。
- 不输出单候选状态，不与 G1A 文件名提示交叉组合。不读取 CAP_FOWNER、不调用 os.access 并宣称能证明 O_NOATIME 权限，也不进一步打开候选试验。全部一致也不能批准 G1B-1 自动重试。
- P1 未获得授权时，P0 不会自动升级至 P1。

## 5. 拟议匿名结果（仅字段定义，绝非生产数值）

成功结果字段：
- stage=52, gate=G1B1_PERMISSION_DIAGNOSTIC, result=ANONYMOUS_OWNER_RELATION_HINTS_ONLY
- root_owner_relation=MATCH|MISMATCH|UNKNOWN
- candidate_count=UNKNOWN_NOT_ENUMERATED（仅 P0）或 11（授权并成功完成 P1）
- owner_match_count/owner_mismatch_count/owner_relation_unknown_count：仅 P1 完成后填匿名计数，否则为 null
- candidate_file_opens=0, candidate_content_bytes_read=0, schema_checked=false, g1b1_retried=false, source_writes=false
- 失败统一 STOP_SCOPE_OR_ACCESS_UNVERIFIED 或 STOP_ROOT_SAFE_ENUM_UNAVAILABLE。不得包含 errno、UID、文件名、路径、栈、进程环境或局部数值。

## 6. 未来开发、测试及验收门禁

- 若之后授权开发，创建另一个独立 Draft PR。使用虚构数据在 Linux/Windows 进行纯合成测试，覆盖根目录及候选所有权一致/不一致、UID 未知、根目录拒绝、符号链接、数量变化、硬链接、元数据漂移、无 atime 保护时停止、零候选文件打开和零内容读取。
- 用 mock 验证无 open(candidate)、无 pread、无 SQLite、无权限修改或环境/私密信息输出。Windows 仅跑纯函数与合成行为，不运行 Linux 生产路径。
- 精确代码 SHA、跨平台合成测试与安全审查通过后，还必须获得**新的生产诊断执行授权**。绝不在 GitHub Actions 挂载/访问 Render 真实磁盘，也不因诊断发现某个 owner mismatch 而自动修改权限。
- G1B-1 继续停止；G1B-2、G1C、G1D 均未授权；Stage50–52 审计 Draft 不合并、不部署。v48 正式验收门槛 36/36、108/108 保持冻结。
