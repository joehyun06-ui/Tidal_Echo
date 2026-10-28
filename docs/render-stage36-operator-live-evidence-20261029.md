# 归汀 Stage 36 — 操作者本机真实只读证据入口（默认关闭）

**本 PR 与 #170–#173 独立；只提供安全的真实证据采集入口，不进行生产调用。**

目前 Render 服务 \`ouou-home-telegram\` 实际仍为 \`33f2fb9e\`，启动 P3、自动部署 OFF、健康检查为 \`/healthz\`，磁盘 \`/var/data\` 1 GB。2026-10-28 14:53 UTC 的单次 502 未归因。项目此前的 #172 已提供固定公网 \`/healthz\` / \`/readyz\` 只读探测工具；这个独立 PR **不重复** HTTP 健康探测，也不引用未合并分支。

## 只在自己可信的 Windows 机器上执行

先在终端切换到本 PR 的工作树。不要将 Bot Token、Render API Key 放在命令行参数、GitHub Actions、日志、PR、聊天或共享的终端录屏中。

此脚本内置精确固定目标：
- Telegram: \`GET https://api.telegram.org/bot<TOKEN>/getWebhookInfo\`
- Render: \`GET https://api.render.com/v1/disks/dsk-d9dk0gbrjlhs73aph4ig/snapshots\`

注意 Telegram 官方 API 规定 Bot Token 出现在 HTTPS 请求路径里；该路径虽然通过 TLS 传输，仍可能进入本地 HTTP 调试/代理日志，所以**不能在不可信网络、代理或调试录制环境中执行**。本脚本不使用环境代理、不跟随重定向、不关闭 TLS 证书验证，不输出原始请求 URL、返回正文或异常。

PowerShell（请在本机独立确认授权，且保留 HTTPS 验证）：

\`\`\`powershell
$env:RENDER_OPERATOR_READONLY_GET_EXPLICITLY_APPROVED = "true"
python scripts/render_operator_evidence_readonly.py --source telegram --perform-read-only
python scripts/render_operator_evidence_readonly.py --source snapshots --perform-read-only
Remove-Item Env:RENDER_OPERATOR_READONLY_GET_EXPLICITLY_APPROVED
\`\`\`

运行时程序分别用隐藏输入提示 Bot Token 和 Render API Key；**不会保存到文件或输出**。两个请求均仅为固定的 GET。每次运行只执行一条请求，不调用 \`setWebhook\`、\`deleteWebhook\`、\`sendMessage\`、快照恢复、部署或配置修改。默认无 flag 或无 \`--perform-read-only\` 时拒绝联网，并且**不读取任何凭据**。

输出只包含非敏感的类别状态：
- Telegram: \`registered_claim_only\` 只是该次官方响应声明已登记匹配目标，并非消息投递成功；\`not_registered\`、\`target_mismatch\`、\`pending_updates\`、历史错误标记可帮助定位问题。最后错误消息正文永不输出。
- Render: \`snapshot_count\`、最新创建时间和 \`recent_metadata_restore_unverified\` 等分类。**不会打印或持久保存** \`snapshotKey\`；出现快照列表不能证明 SQLite 备份一致性、磁盘真正可恢复或旧部署与数据库 schema 兼容。Render 快照恢复是不可逆的 POST 操作，本工具绝不包含此能力。
- DNS/TLS/限流/认证失败返回安全的 \`unavailable\` 或 \`http_error\` 分类，不暴露令牌或原始异常。
- 不论任何结果，\`may_deploy\`、\`may_rollback\`、\`may_change_telegram\`、\`may_restore_disk\`、\`may_enable_oidc\`、\`may_write_memory\`、\`may_use_paid_api\` 永远为 false；退出码固定为 2，不充当生产发布许可。

只应分享这些脱敏分类摘要，不要传完整的 Telegram API 原始结果、完整 webhook URL、原始报错或快照 key。

## 可由助手继续的只读工作

Render 连接器可复查当前实例、事件与指标，但**未提供快照列表工具或安全的 Bot Token 读取接口**。当前执行容器 DNS 解析 Render 服务域名失败，网页读取也无法取得 \`/healthz\` 或 \`/readyz\`。不能据此推断服务宕机。建议操作者在自己的联网电脑使用 #172 的健康探测工具，在本 PR 的独立工具采集 Telegram 与 Render 快照摘要，然后将**脱敏结果**交给审计。

官方说明：
- https://core.telegram.org/bots/api#getwebhookinfo
- https://api-docs.render.com/reference/list-snapshots
- https://render.com/docs/disks
- https://render.com/docs/rollbacks

## 安全与验收边界

GitHub CI 只运行离线 Mock，不启用真实 HTTPS GET、没有任何 Bot Token/Render API Key。真实请求必须通过本地人工双重许可。即使所有离线测试通过，也不代表三项线上证据已经采集，不能改变 v48 正式 **36/36、108/108** 冻结标准，不能据此合并/部署或回滚。真实 OIDC、记忆写入及付费 API 继续禁止启用。
