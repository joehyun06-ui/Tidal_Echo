# 归汀 · 真实 Render 公网健康检查与 Telegram/持久卷恢复证据（第 34 阶段）

**工作方式：只读审查、独立 Draft、不改服务、不调用 Telegram Bot API，不合并、不触发发布/回滚。**

## 当前已核实的真实状态

用户此前确认的 Render 工作区 `ouott`、服务 `ouou-home-telegram`。2026-10-28 只读 `get_service` 与 `list_deploys` 仍确认 live `dep-dbh0mv6tlv9c73dj16mg` / 合并提交 **`33f2fb9e09d42a901728355720b8334054c7fab5`**。启动 `python scripts/render_start_p3.py`，自动部署 `off`，维护模式关闭，健康路径 `/healthz`，Starter/Oregon，1 个实例，持久磁盘 `ouou-home-data` 1 GB、挂载 `/var/data`。旧部署 `dep-dbdd6ld9fdbs73anfue0` / `a2d8699a` 已是 `deactivated`，但**旧构建产物是否仍可用于回滚尚未确认**。

本轮已尝试通过运行容器对 `https://ouou-home-telegram.onrender.com/healthz` 发起**单次无认证 GET**；命令返回 DNS 解析失败（`curl` status 6，HTTP 状态 `000`），未取得服务响应。网页检索接口也未能打开该 URL。这是**当前审计执行环境无法验证生产端点**，不是证明 Render 停机或健康。没有使用 `--insecure`、自定义代理、跳过 TLS 或替代 IP，没有向 Telegram 发送请求。

## 重要发现：/healthz 不是 /readyz

在当前真实部署提交的 `backend/app.py` 中：
- `GET /healthz` 仅返回 `{"ok": true}`，不检查 API Loop、数据库或 Telegram worker。Render 的 `healthCheckPath` 只指向这一路径。
- `GET /readyz` 才检查数据库 schema、持久化路径、`brain_target`、Telegram 配置和 worker、loop nonce/内部健康、memory core，以及按配置启用的更多业务模块。就绪时返回 200 和 `{"ready":true, "status":"ready", "checks":...}`；不就绪时返回 503，部分检查含固定安全错误分类。
- 因此部署平台标记 `live`、`/healthz` 返回 200（即使未来真实测到）和**业务全流程就绪**三者不能等同。请求 `/readyz` 只作诊断观察，不能作为用户身份或数据权限证明；失败字段不可当秘密值输出至公共报告。

Render 官方健康检查文档：https://render.com/docs/health-checks 。

## 新增固定端点的本机只读探测工具

`backend/render_public_health_probe_readonly.py` 与 `scripts/render_public_health_readonly.py`：

- 默认完全不联网。只接受值为严格字面量 `true` 的 `RENDER_READONLY_HEALTH_GET_EXPLICITLY_APPROVED`，需操作者在**自己的安全联网环境**主动设置；GitHub CI 与本次对话没有启用它。
- 精确固定 `https://ouou-home-telegram.onrender.com/healthz` 或 `https://ouou-home-telegram.onrender.com/readyz`，不接受自定义 Host、URL、用户信息、路径参数、重定向或 Telegram webhook 路径。
- 仅用 GET、不发送 Cookie、Authorization、Bot Token、POST；不信任系统代理环境、保持默认 TLS CA/主机名校验；超时 5 秒，读取上限 4096 字节，JSON 字段和值白名单，不打印返回正文或原始异常。
- `/healthz` 仅把精确 200 + `{"ok":true}` 归类为 `http_observed`；`/readyz` 只允许有限的既有 readiness 布尔状态，不打印敏感错误数据，503 以响应状态记录而不是对用户资料做操作。
- 即使观察成功，所有 `may_deploy`、`may_rollback`、`may_enable_oidc`、`may_write_memory`、`may_use_paid_api` 永远为 `false`；CLI 固定返回非零退出码 2。**工具没有上线、回滚、Telegram 或记忆写入功能**。

仅在已获许可的**用户本机网络**执行（分别检查，建议由操作人员确认无敏感错误数据暴露）：

```bash
RENDER_READONLY_HEALTH_GET_EXPLICITLY_APPROVED=true \
python scripts/render_public_health_readonly.py --endpoint healthz

RENDER_READONLY_HEALTH_GET_EXPLICITLY_APPROVED=true \
python scripts/render_public_health_readonly.py --endpoint readyz
```

Windows PowerShell：

```powershell
$env:RENDER_READONLY_HEALTH_GET_EXPLICITLY_APPROVED = "true"
python scripts/render_public_health_readonly.py --endpoint healthz
python scripts/render_public_health_readonly.py --endpoint readyz
Remove-Item Env:RENDER_READONLY_HEALTH_GET_EXPLICITLY_APPROVED
```

若失败显示 `unavailable`，需区分本机 DNS/TLS/出站限制与实际服务故障；不应直接回滚。

## 502 与 Telegram webhook：下一个需要真实证据的地方

只读 Render 指标在 2026-10-28 14:53 UTC 记录到一次 502；15:43 UTC 的另一指标桶记录到 7 次 200，但**没有请求路径、客户端或错误因果关系**。已查询的 Render 请求日志与应用错误日志没有提供对应行，不能归因。502 不能忽略，也不能单凭一次 502 就实施回滚。

真实 Telegram webhook 是否有效**尚未验证**：Render `/readyz` 通过仅表明内部 Telegram worker 配置/存活检查通过，不代表 Telegram 云端已登记正确 webhook，亦不能证明消息端到端可投递。要核验云端状态，须在秘密不外泄的受控本机流程中，以只读方式查看 Telegram 官方 `getWebhookInfo` 的**摘要字段**：目标域名/精确 webhook 路径是否匹配、`pending_update_count`、`last_error_date` 与安全分类后的 `last_error_message`；不可在 PR、聊天、日志中输出 bot token、URL 查询密钥、消息内容、用户 ID 或完整异常信息。

仓库已有 `scripts/configure_telegram_webhook.py` 调用的是 **`setWebhook`（POST，会修改 Telegram 状态）**，本轮没有执行，也不应该当成只读核验工具。没有 Telegram webhook URL 的授权证据前，不执行 webhook POST 测试。可再通过只读的 Render 请求指标及用户许可的受控端到端测试确认投递链。

## /var/data 磁盘与回滚前提

Render 服务的持久数据盘仍挂载在 `/var/data`，仓库 Blueprint 记载数据库 `/var/data/relay.db`、上传目录 `/var/data/uploads`、brain 目标和 API Loop 配置路径。**只读服务元数据不能证明盘内数据库的真实 schema、文件完整性、快照是否存在、旧构建产物是否保留。**

正式回滚以前必须独立确认：
1. 真正存在持续业务异常，并完成与 502 时间、路径、Telegram 实际交付状态的关联；
2. 旧部署 `a2d8699a` 的构建产物仍可用，以及目标版本和当前数据库/记忆 schema 兼容；
3. `/var/data` 的最新可恢复快照与数据备份，恢复计划经核准；不要把部署回滚当成磁盘回滚；
4. 由用户明确批准后才通过 Render 控制台或授权 API 进行回滚；回滚后继续检查自动部署为 OFF、健康状态、数据及 Telegram webhook。

Render 官方回滚说明：https://render.com/docs/rollbacks 。

## 保持冻结与职责边界

本 PR 独立建立在 #169 已上线合并提交上，不依赖仍为 Draft 的 #170、#171。新增脚本、纯离线测试及文档**没有改变生产服务或 Render 配置**；既有 v48 **36/36 和 108/108** 正式验收门槛保持冻结。真实 OIDC 可信边缘证明、身份提供商、用户同意、记忆写入与付费 API 的启用许可仍未获得。不能从部署成功或单次健康 HTTP 结果推断它们已安全开启。
