# 归汀 · #169 上线后的真实 Render 运行审计与回滚准备（只读）

**审计时间窗：2026-10-28 14:45–15:43 UTC；性质：只读、无在线请求、未授权发布/回滚。** 由已确认用户工作区 `ouott` 的 Render Connector `get_service`、`list_deploys`、`get_deploy`、`list_events`、`list_logs` 和 `get_metrics` 取得元数据，再将白名单字段整理成仓库中的 `docs/evidence/render-post169-readonly-20261028.json`。JSON 是人工归档，**不是** Render 签名的证据或实时接口，也不是可信代理身份凭据。原始环境变量、完整日志正文、请求载荷、用户标识及 API Key 均未归档。

## 1. 真实部署版本与入口

- 服务 `ouou-home-telegram`，分支 `feat/render-telegram-deployment`，一台 Starter/Oregon Web 实例，`PORT=10000`（服务公开监听端口 `10000/TCP`）。
- `dep-dbh0mv6tlv9c73dj16mg` 已部署提交 **`33f2fb9e09d42a901728355720b8334054c7fab5`**，2026-10-28 **14:49:32 UTC** 开始、**14:51:19 UTC** 完成，状态 `live`、触发方式 `api`，Render 事件中的部署完成状态 `succeeded`。这是 PR #169 的合并提交。
- 之前的成功部署 `dep-dbdd6ld9fdbs73anfue0`、提交 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`，现在为 `deactivated`；还存在更早的成功部署，**没有确认其回滚构建产物仍受保留**。
- 实际启动 `python scripts/render_start_p3.py`，`autoDeployTrigger=off`，健康检查 `/healthz`，维护模式关闭、服务未暂停，默认 `onrender.com` 子域启用，公网 IP 规则 `0.0.0.0/0`。
- Draft #170 拟纠正旧 `render.yaml` 使用 `python scripts/render_start.py` 的漂移，但**#170 未合并/未部署**；不要将该 PR 的变更归入当前 live。
- #168 的只读发布元数据核对器依然是另一个独立 Draft，**未作为 #169 的合并范围发布**。

## 2. 实际启动、运行与错误观测

使用只读 Render Metrics / Events / Logs：

| 观测维度 | 记录 | 可以推出什么 / 不能推出什么 |
| --- | --- | --- |
| 14:49–14:51 部署 | `build_ended=succeeded`、`deploy_ended=succeeded` | 平台已标记发布成功；不表示业务功能完整 |
| P3 supervisor 事件 | 旧进程 `SIGTERM`、`api_loop` 停止；新 `api_loop started/ready`、`relay started` | 支持 P3 子进程已启动；不提供单次 HTTP 客户端来源证明 |
| Instance Count 指标 | 14:51 UTC 采样为 **0**；14:53 及后续至 15:41 采样为 **1** | 短暂缺口可能与部署替换有关，但没有建立因果关系；亦不能代表每一秒均可用 |
| HTTP 状态指标 | 14:53 UTC 的采样桶有 **1 次 502** | 真实异常信号，必须列入监控与事件关联；不能假定它只是部署抖动 |
| 更晚的 HTTP 指标 | 15:43 UTC 的采样桶有 **7 次 200** | 不知道 URI / 请求主体或用户场景；不能宣称 Telegram webhook 或 `/healthz` 正常 |
| filtered `/healthz` request log | 返回 **0 条** | 既不是失败证明，也**不是**成功证明；受日志覆盖、日志类型和套餐限制影响 |
| filtered 502 request log | 返回 **0 条** | 不能归因；保留 502 指标作为独立事实 |
| filtered app error/critical logs | 返回 **0 条** | 不能将日志空白解释成不存在错误 |
| Render server_failed / server_restarted 等事件 | 本查询的过滤时间窗内 **0 条** | 只是该事件查询结果，不是绝对无故障保证 |

Render 官方指出，单实例加持久磁盘的部署不享有正常零停机部署保证，旧进程更换期须关注监控缺口。Render 的 HTTP 健康检查要求 2xx/3xx 响应；仅仅配置了 `/healthz` 路径并不能证明当前健康探测结果。参考：https://render.com/docs/deploys 和 https://render.com/docs/health-checks 。

**本轮没有对生产 URL 发起新的 GET/POST，也没有读取任何生产 OIDC/付费 API 密钥；不能由此宣称真实浏览器登录、代理可信来源或记忆写入已经验证。**

## 3. 回滚预案：准备好目标，不代表可直接回滚

目标候选仅为历史上成功的 `dep-dbdd6ld9fdbs73anfue0` / `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`；**不是**让其立即上线的建议。真实回滚前由用户单独批准，并执行：

1. **先判定是否需要回滚**：确认最新部署的业务错误是否持续，关联 502 指标、Telegram webhook 的实际无副作用查询/人工测试、P3 relay read-only 健康、容量、告警、请求日志覆盖，记录精确时间。单次 502 不足以单独触发回滚。
2. **确认旧产物可用**：在 Render Deploys 查看历史成功部署是否显示可执行的 `Rollback`；Render 仅保留有限数量的构建产物，不能凭 `deactivated` 推断可回滚。
3. **独立核对持久磁盘**：确认挂载 `/var/data` 的备份/快照、是否有新版本写入的数据和迁移、回滚程序是否能读取现有数据库，保存检查与恢复计划。**Render 回滚不能还原持久磁盘。** 未完成该核对时，不进行回滚。
4. **确认配置与开关**：比较目标部署时的启动命令、健康路径、环境变量组与当前服务配置差异；检查是否意外打开 OIDC/记忆/付费 API，且不要把任何秘密值输出到审计文件。
5. **独立获得发布/回滚批准后才操作**：Render Dashboard 服务的 Deploys → 找到目标成功部署 → Rollback → Rollback to this deploy；或由具有权限的部署工具单独执行。不要在测试/审计脚本里调用 rollback 或 trigger deploy。Render Dashboard 回滚通常关闭自动部署，而 API 回滚不会自动关闭；当前服务 `autoDeployTrigger=off`，回滚前后仍需再确认。
6. **操作后重新验收**：对照新的部署 ID、live SHA、运行时 P3 启动日志、`/healthz` 与核心 webhook、监控 5xx、持久卷数据完整性，并明确人工决定继续运行还是恢复发布。必须记录新部署时间、回滚结果和负责人的批准，不宣称原状态自然恢复。

官方：[Render Rollbacks](https://render.com/docs/rollbacks)。特别注意：Render 回滚可复用旧构建产物以及目标部署的部分命令/环境变量，**但不会将持久磁盘、所有域名或平台配置恢复到历史状态**。

## 4. 归档规则与自动化的责任边界

- 原始实时来源为 Render 连接器只读查询；仓库证据 JSON 仅存储非秘密、允许字段。它不包含可作为授权凭证的请求日志、套接字来源、TLS 证明或凭据。
- `backend.render_live_observation_review_offline` 仅严格验证归档记录结构，并区分 `live`、502、后续 200、不完整健康日志、缺少回滚产物/磁盘核对等证据。即使没有 502，也永久保留生产上线阻断。
- `scripts/render_live_readonly_audit_check_offline.py` 仅接受小于等于 8 KiB 的本地 JSON、输出固定的非敏感类别，并始终退出代码 **2**：**永不批准部署、回滚、OIDC、记忆写入或付费 API**。
- Windows/Linux 21 项专用离线测试，对假冒凭据、伪 TLS 证明、错误部署 ID、重放过期 SHA、非法字段等保持 fail-closed。离线测试不证明最新 Render 状态。
- 可复核指令（只检查静态归档）：

```bash
python scripts/render_live_readonly_audit_check_offline.py \
  --snapshot docs/evidence/render-post169-readonly-20261028.json \
  --expected-live-sha 33f2fb9e09d42a901728355720b8334054c7fab5
```

## 5. 真正上线仍未通过的条件

- **业务发布**：#170 提议的 Blueprint 对齐，以及本 PR 的审计产物均仍是 Draft；需分别审批，不能由测试全绿而自动部署。
- **独立 OIDC / 用户记忆发布**：Render/Cloudflare 逐请求 TLS/peer、转发头清洗、可信 Host/Origin/callback、生产 IdP/JWKS/PKCE、浏览器会话撤销与用户同意、记忆写入授权仍未实证。禁止将 #166/#167 的合成 proof 改成常量 True。
- **付费 API**：仍不进行真实调用或启用。
- **v48 冻结验收**：36/36 与 108/108 作为正式旧版门槛保持不变，不由本轮任何 502 或运行数据替代。

**审计完成 ≠ 发布许可 ≠ 回滚许可。**
