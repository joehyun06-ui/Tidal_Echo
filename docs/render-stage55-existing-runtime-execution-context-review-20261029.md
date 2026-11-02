# 归汀 Stage55 — 既有生产运行上下文证据核查（只读控制面与冻结源码）

> **状态：REVIEW_ONLY / NO_APPROVED_EXECUTION_CONTEXT / NO_PRODUCTION_DISK_READ。2026-10-29。**
> 仅审查已部署精确版本的 GitHub 源码、Render 服务与部署元数据，以及 Render 官方文档；不运行 shell、不开 `/var/data`、不读文件元数据/内容、不新增管理入口、不修改生产配置。
> 独立 Draft PR 只增加本 Markdown；它不是部署、恢复、SQLite 读取、P1 检查或权限变更的授权。

## 1. 版本锚点与证据等级

- Render connector `get_service`：服务 `ouou-home-telegram`，类型 web_service，单实例，当前实际配置启动命令 `python scripts/render_start_p3.py`，持久磁盘挂载 `/var/data`，分支 `feat/render-telegram-deployment`，`autoDeployTrigger=off`，`suspended=not_suspended`。
- Render connector `list_deploys`：最新 deployment 状态 LIVE，提交 `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`。此类证据不含运行中进程 eUID、CAP_FOWNER、目录 owner、文件系统类型或原始数据库内容。
- GitHub **精准以该 deployed SHA** 读取的源码：`scripts/render_start.py`、`scripts/render_start_p3.py`、`backend/p3_relay_app.py`、`backend/p3_codex_relay_app.py`、`backend/codex_canary_admin_proxy.py`、`backend/codex_canary_recovery_admin.py`、`backend/memory_operator_cli.py`、`backend/deployment_config.py` 和 `render.yaml`。
- 仓库 `render.yaml` 的 `startCommand` 仍为 `python scripts/render_start.py`，而**实际 Render 服务控制平面**的启动命令为 `python scripts/render_start_p3.py`。两者发生声明漂移；本次不更改任何一方，更不能以仓库模板证明实际的进程或环境配置。
- Stage51 G1B-1 owner-returned `STOP_READ_NOT_SAFE`；Stage52 P0 owner-returned根目录 owner 与审计 SSH 进程 eUID 为 `MISMATCH`；Stage53 P0C owner-returned后续 Python 线程有效 `CAP_FOWNER=NO`。**不同时间的进程**不能被视为同一个进程的 syscall 证据。
- Stage54 PR #194 选择 `HOLD_NO_SAFE_PATH_PROVEN`；本轮检索的是候选既有执行通道，而非试图重新运行原程序。

## 2. 当前生产运行组件的静态结构与入口

| 候选运行组件/入口 | 冻结源码证据 | 对 G1B-1 的判断 |
| --- | --- | --- |
| `scripts/render_start_p3.py` 生产 supervisor | 由 Render 实际 startCommand 启动；其选择器委托 `render_start.child_commands`，负责检查 P3 代理与授权开关 | 存在通用 supervisor，**不等于**已提供专用数据库审计任务或 owner-matched 执行身份 |
| `scripts/render_start.py` 子进程管理 | `child_commands` 构造 Uvicorn 的 API loop/relay 启动命令；`RenderSupervisor._start` 通过 `subprocess.Popen(command, env=..., start_new_session=...)` 启动进程；该调用未指定 `user`, `group`, `preexec_fn`, `setuid`, `setgid` 或 capability 选项 | 这些路径没有独立的所有者匹配切换机制；**不能**从代码推断实际进程 eUID、磁盘 owner 或 POSIX 访问资格 |
| `backend/p3_relay_app.py` 生产入口 | 安装业务状态/会话、Provider、Memory 相关路由；不是任意本地审计脚本执行器 | 没有发现已授权的 Stage55/SQLite 文件头审计 route；禁止新建公网管理路由来绕过权限 |
| `backend/p3_codex_relay_app.py` 可选 Codex 入口 | 条件选择下配置 Codex 集成及 Ledger 路由；P3 入口与 canary admin 路由不同 | 不能作为拥有更高文件权限的证据，也不允许启动/切换该入口用于数据取证 |
| `backend/codex_canary_admin_proxy.py` 与 `codex_canary_recovery_admin.py` | canary 业务动作需现有认证，并包含操作数据库/恢复任务语义 | 与本次 11 个未知候选不相符；不作为只读无副作用诊断入口 |
| `backend/memory_operator_cli.py` | local-only one-shot Memory CLI，命令作用域仅 Memory status/validate/remember/correct/forget 等；写入类具有独立授权边界 | 不提供任意候选 SQLite 文件头受限访问；禁止复用到本次 G1B-1 |
| 原已使用的 Render SSH | 当前脚本由该 SSH 启动；P0/P0C 观测其不同时间的身份关系与能力 | 原 SSH 通道继续 **STOP**；不得用 sudo、`su`、capset、chown、chmod 或移除 O_NOATIME 重试 |

静态代码审查结论仅覆盖所检查的冻结版本文件和上述入口；**不等于证明其他未知进程/接口不存在**。但无可审计证据证明任何额外通道同时具备授权、磁盘可见性、合法身份及零源修改属性。

## 3. Render 受控通道与持久磁盘的约束

- Render 官方 SSH 文档区分普通 SSH 到正在运行的服务实例，以及新建的独立 ephemeral shell instance。后者不应被当成原持久磁盘所有权或文件可见性证明。
- Render Persistent Disks 文档：磁盘只由单个服务实例在运行时访问；build、pre-deploy、其他服务或 one-off job 无法访问已附加的原磁盘。故不采用 job、构建钩子、预部署或临时实例作为未经验证的替代通道。
- Render 官方文档允许通过 SCP 转移原磁盘文件，但此操作会复制/导出真实文件，属于当前 G1C 数据处理边界之外，不是 G1B-1 零复制审计。
- 既有服务运行进程即使能够访问其业务数据库，也不意味着可以在不修改代码、部署或变更权限的情况下运行隔离的匿名 100-byte 文件头审计。

## 4. 证据矩阵（证明 vs. 未证明）

| 门禁 | 现在是否有充分证据？ | 判定说明 |
| --- | --- | --- |
| LIVE 同一实例挂载 `/var/data` | 部分：控制面显示单实例和磁盘挂载 | 证明服务拓扑配置，不证明可控审计代码运行身份 |
| 现存、明确授权且不改变服务运行行为的执行入口 | **否** | 已审阅入口未发现 G1B-1 专用 audited job/handler |
| 执行上下文 eUID 与根目录及 11 候选 owner 兼容 | **否** | P0 是原 SSH eUID 不匹配；其他进程没有实时所有权证据 |
| 执行上下文不提权、不重启、不部署、不注入 | **否** | 没有证据展示既存可用入口达到全部要求 |
| `/var/data` 特定挂载的 O_NOATIME/目录枚举零 atime 影响 | **否** | 既未获真实文件系统证明，也不应再次探测生产 |
| 11 候选数量、类型和范围稳定 | **未知** | Stage50 较早用户输出，不代表 Stage55 此刻快照 |
| 数据库身份或完整性得到证实 | **否** | G1A 名称无提示，G1B-1 因安全停机未完成 |

**Stage55 判断：`NO_APPROVED_EXECUTION_CONTEXT_FOUND_IN_REVIEWED_EVIDENCE`；继承 Stage54 `HOLD_NO_SAFE_PATH_PROVEN`。**
这里的 FOUND_IN_REVIEWED_EVIDENCE 是证据有限性的说明，不可扩大为系统上永久不存在任何合法途径。

## 5. 后续升级条件（仅设计，不触发）

若将来另有可信平台/运行手册提供既存受控进程身份入口，必须先独立评审：

1. 先用官方运行契约证明入口存在且位于原磁盘同一服务实例，能够以合规身份执行**准确固定 SHA**的受限审计程序，无新 HTTP 路由、IPC、执行注入、额外子进程提权、环境修改、启动命令修改、重启或部署。
2. 必须单独获得当前环境所有权、有效能力与文件系统 O_NOATIME 行为的严格证据；不能由 Stage52/53 跨进程结果推测。新生产元数据检查需要**新的精确授权**。
3. G1A 的严格 11 个候选 + 4 个目录限制、符号链接/硬链接与漂移保护、只读每文件最多 100 byte、不打开 SQLite 及 WAL/SHM、仅匿名计数等继续不变。
4. 在完全虚构文件和权限上下文中完成 Linux/Windows 合成验证，再审查精确代码 SHA、错误输出、零副作用及审计责任；单独得到新的 G1B-1 执行许可后才可操作。
5. 任一条件不可确认，立即停止并记录 `HOLD_NO_SAFE_PATH_PROVEN`。不能用普通 O_RDONLY、提升权限、修改 owner、复制/备份、Render job 或更新服务配置替代证明。

## 6. 处置与冻结边界

- 本阶段唯一实际仓库修改是这份文档；所有审计 PR 保持 Draft、未合并，不触发部署；Render 服务的 autoDeploy OFF 与 LIVE 原基线不变。
- 当前不提供 SSH 命令，不要求用户再执行 UID/能力/权限查询；既有 G1B-1 STOP 保持，P1/G1B-2/G1C/G1D 继续锁定。
- v48 正式验收 36/36、108/108 门槛不因本设计产生完成声明。

## 7. 参考与溯源

- Render 当前控制面：服务 get_service 与最新 list_deploys 的只读核对（2026-10-29）。
- Render Persist Disks: https://render.com/docs/disks
- Render SSH/Shell: https://render.com/docs/ssh
- Render One-Off Jobs: https://render.com/docs/one-off-jobs
- Stage54 PR #194；Stage53 P0C PR #193；Stage52 P0 PR #191；Stage51 G1B-1 PR #189。
