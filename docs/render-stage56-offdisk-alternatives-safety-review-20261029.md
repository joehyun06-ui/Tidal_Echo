# 归汀 Stage56 — 不访问生产磁盘的替代路径可行性与安全授权矩阵

> 2026-10-29；**DESIGN_ONLY / ZERO_INTENTIONAL_PRODUCTION_DISK_ACCESS / NO_BACKUP_OR_RESTORE**。
> 本 PR 仅新增一份 Markdown 风险评估。无真实快照列表查询、快照创建/恢复、磁盘挂载、SCP、文件下载、复制、schema/content read、服务重启或部署；无 GitHub CI 对真实数据的访问。
> “无需访问生产磁盘”限制的是本阶段的实际操作。列出的未来潜在方案并不因纳入表格而自动获得权限。

## 1. 证据锚点、假设和来源限制

- Render 控制面只读核查：`ouou-home-telegram`，单实例、服务仍 LIVE，最新部署 `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`，自动部署 OFF，持久磁盘挂载 `/var/data`，当前控制面启动命令 `python scripts/render_start_p3.py`。这些字段不等于文件系统的证据。
- Stage50 G1A 用户返回匿名 11 个 SQLite 扩展名候选 + 4 个未知目录，11 个名称线索均不分类，不证明有效 SQLite、用途或任何备份身份。
- Stage51 G1B-1 用户返回 `STOP_READ_NOT_SAFE`，不能证明旧进程零字节读取或旧 syscall 的精确 errno；此前已暂停，不得重试。
- Stage52 P0 用户输出 root owner `MISMATCH`；Stage53 P0C 用户输出当前线程 effective `CAP_FOWNER=NO`，强烈支持目录 `O_NOATIME` 权限条件不满足的假设，但并非同一时刻同一进程的直接错误证明。
- Stage54 PR #194 决策 `HOLD_NO_SAFE_PATH_PROVEN`；Stage55 PR #195 未在**已检查的**生产源码/控制面证据中找到既有、被授权且无提权的所有者匹配审计入口。
- Stage56 **没有查询 Render 真实磁盘 ID、快照清单、快照 payload、快照可用性或数据库内容**。Render 的一般平台特性不证明此用户磁盘此刻一定存在可用快照。

## 2. Render 已公开能力与不能跨越的边界

- 官方 Persistent Disks 文档说明：Render 对持久磁盘自动创建约每日一次快照，并保留约 7 天；此为通常的平台机制，并不说明某个具体 disk 已成功存在某个时间点快照。即使有快照，**也不保证 SQLite 逻辑一致性**。
- 官方只提供原磁盘的整盘快照恢复能力；**不能只恢复磁盘的指定文件**。恢复到原盘会覆盖目标快照之后的变更；恢复 API 明确警告可能触发服务部署，操作不可逆，绝不作为只读审计手段。
- Render API `GET /v1/disks/{diskId}/snapshots` 可只读列出 snapshot 的 `createdAt`、`snapshotKey`、`instanceId` 等元数据；它仅证明控制面存在快照记录，**没有文件列表、SQLite 文件头、Schema 或数据库有效性数据**。不得泄露/持久化 snapshotKey、diskId 等可用于后续操作的敏感标识；实际调用仍需单独批准和合规 connector 能力。
- 本会话所连接的 Render 工具没有用于列出磁盘快照元数据或读取镜像内容的对应只读动作；**本轮没有尝试用未知 API 替代或以写接口绕过**。
- 官方 SCP/SFTP 支持与 live disk-backed service 之间传输文件，但这会读真实生产文件并复制到本地；不符合 G1B-1 私有、100 字节、无数据导出边界，且可能触及文件访问时间/并发快照一致性。属于独立数据复制风险评审，不是‘无需访问生产磁盘’。
- 官方 one-off jobs、build/predeploy 在不同计算环境执行，不能访问已附加的原持久磁盘；SSH ephemeral instance 不能假定看到原盘，因而不能凭空获得相同数据。
- Render sandbox snapshot 属于另一产品类型，不是 Render web service persistent disk 的快照；Render Postgres PITR/export 也不是此本地 SQLite 候选文件的自动可用备份方案。不得混用不同资源产品的能力。
- SQLite 官方在线备份 API 可产生数据库层一致性副本，但它需要连接源 SQLite 数据库并向其他位置复制数据，可能涉及锁、WAL 等；当前未授权 G1C，不作为现有零源修改 G1B-1 的替代执行路径。

## 3. 替代方案比较

| 备选 | 访问真正生产磁盘 | 创建/复制/改变真实数据 | 能否证明 11 个 SQLite 身份 | 目前决策 |
| --- | --- | --- | --- | --- |
| A. 读取现有 GitHub/Render 控制面配置、已审计的匿名汇总 | 否 | 否 | 否 | **继续允许只读设计研究** |
| B. 仅查询已有 Render 磁盘快照元数据 | 不访问文件内容；依赖 Render 控制面 | 不创建/复制 payload，但需审查权限与敏感 token | 否 | **仅保留可行性，未来独立批准** |
| C. 读取某个快照的文件目录/文件头 | 必须有受支持、隔离的快照读取/挂载通道；目前没有已证实通道 | 可能需要创建独立副本并读取敏感数据 | 仅能得到对应历史快照的格式提示 | **未获证据；不得执行** |
| D. 恢复 Render 自动快照到原生产盘 | 直接替换磁盘内容 | **破坏性覆盖**，可能引发部署 | 否，且会损害生产数据 | **绝对禁止在此阶段执行** |
| E. SCP/SFTP/归档/在线 SQLite Backup | 是 | 导出完整真实文件/数据库内容 | 可供另获授权的离线调查，但不等于当前身份验证 | **G1C 未授权** |
| F. One-off job / build / predeploy / ephemeral shell | 按官方隔离模型无法假定能访问原盘 | 可能创建额外计算或环境副作用 | 否 | **不采用** |
| G. 维持冻结、不做真实数据动作 | 否 | 否 | 否 | **当前选择** |

## 4. 对任何未来离线副本方案的新增威胁模型（不是授权）

1. **数据来源绑定**：必须有可信快照/导出来源的原始 disk、snapshot time、instance、服务版本与一致性证明，且只在授权的信任域内处理。无来源证明则不可将副本当作 Stage50 的 11 个候选。
2. **时点风险**：Render 自动快照是某个时间点的整盘视图，可能早于 Stage50 的候选枚举，无法据此推断 11 个文件在该历史时点存在或数量相同；G1B-1 若作用于快照，只能报告那个快照的**历史**信息，不是实时生产数据库验证。
3. **SQLite 一致性**：跨页/并发写事务和 WAL 需要额外数据库级审核；文件系统快照存在不等于 SQLite 数据库备份有效。不能直接查询 Schema、执行 integrity_check 或打开未知 .sqlite 文件。
4. **隐私**：匿名计数以外的任何原始文件名、目录、文件头、哈希、密钥、UID、记录、snapshotKey、下载 URL 等都不得出现在 GitHub、ChatGPT、普通日志和非必要 CI 工件中。
5. **最小复制**：完整快照/文件导出与当前受限 100 字节目标不成比例；若不得不复制，必须作为新 G1C 方案独立批准，明确来源、访问、存储期限、销毁及禁止提交 GitHub/CI。
6. **零源修改**：不读取原盘可以避免由该检查主动触发的 atime 操作，但**不能**声称 Render 的自动快照系统对底层服务没有任何影响或该快照必然具有数据库一致性。
7. **恢复隔离**：若想在隔离环境中读取历史副本，需要先证明 Render 公开支持非破坏性的独立克隆/导出接口；本轮没有这种证据。禁止借‘先恢复再看’替代。

## 5. 最小进一步研究门禁（分级，不执行）

### M0：纯文档/控制面研究（本阶段完成）

- 只读核对 Render 控制面服务、部署、磁盘声明与官方 API 文档，不调用磁盘/快照 API，不访问任何持久磁盘文件；不发 SSH。

### M1：仅已有快照的只读元数据列举（尚未授权）

- 若未来确有必要且有**官方受支持的只读连接器**，另行批准只允许 `GET /v1/disks/{diskId}/snapshots` 的单次列表查询。确认 API 作用域、凭据最小权限、敏感 snapshotKey 不显示/存储，异常匿名处理。不得调用 create/restore/delete/download/clone 等写或 payload 接口。
- 返回仅为 `snapshot_count`、是否有最近快照的匿名布尔项及过期/未知状态；**不**输出 key、diskId、instanceId、时间序列或任何文件内容。若没有可用只读工具则停止，不要求用户提供密钥。

### M2：非破坏性独立历史数据材料（尚未授权）

- 只有平台提供真实受支持的**独立只读快照克隆/导出**通道，且 G1C 数据处理、隐私、加密和一致性威胁模型得到另外授权后才能再设计。它不再属于无生产数据接触的本阶段。
- 即使以后获得离线材料，也需区分 `HISTORICAL_SNAPSHOT_FORMAT_HINT` 与实时 `G1B1_HEADER_CLASSIFICATION_ONLY`；数据身份验证与 G1D 恢复仍然不可自动升级。

## 6. Stage56 正式结论与持续冻结

- **`OFFDISK_METADATA_RESEARCH_POSSIBLE_BUT_NOT_EXECUTED`**：Render 官方的已有快照只读列表 API 可能提供有限的**控制面存在性信息**，但不能证明内容或有效 SQLite，且没有本次连接器可用的相应读能力。
- **`NO_SUPPORTED_NONDESTRUCTIVE_SNAPSHOT_CONTENT_PATH_PROVEN`**：没有证据表明可以在不修改原盘、未获 G1C 的情况下从 Render 快照直接查看某个 SQLite 文件头。
- **`HOLD_NO_SAFE_PATH_PROVEN`**：继续原 Stage54/55 决策，不再重跑 G1B-1；P1/G1B-2/G1C/G1D 继续锁定。
- 不触发任何快照创建/恢复或生产部署，所有审计 PR 继续 Draft/未合并；v48 正式 36/36、108/108 门槛不变。

## 7. 官方参考与来源

- Render Persistent Disks（快照、恢复限制、SCP 和访问隔离）：https://render.com/docs/disks
- Render one-off jobs（不访问原磁盘）：https://render.com/docs/one-off-jobs
- Render SSH 和 ephemeral instances：https://render.com/docs/ssh
- Render API list snapshots：https://api-docs.render.com/reference/list-snapshots
- Render API restore snapshot（不可逆、覆盖原盘）：https://api-docs.render.com/reference/restore-snapshot
- SQLite Online Backup API：https://sqlite.org/backup.html
- SQLite WAL：https://sqlite.org/wal.html
- 关联审计：Stage55 PR #195、Stage54 PR #194、Stage53 PR #193、Stage51 PR #189。
