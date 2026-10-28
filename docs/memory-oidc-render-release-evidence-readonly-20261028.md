# 归汀 · Render 生产发布证据核对（2026-10-28；只读、默认 OFF）

本阶段堆叠 #167 已验收 Draft（31/31 工作流、59/59 Job；Chromium / WebKit 实测 11/11 各自通过；Linux Python 2007/2007）。与继续添加“模拟可信代理”不同，本轮重新使用已经由用户确认的 Render 工作区 `ouott` 的只读接口，核对 **live 版本 / 启动入口 / Blueprint / 域名和公网来源设置**；然后将差异落实为**不可自行解锁上线**的核对工具。

## 2026-10-28 只读源数据

经用户确认的 Render 工作区 `ouott`，服务 `ouou-home-telegram`，仓库 `joehyun06-ui/Tidal_Echo`；`get_service` / `list_deploys` 均来自已连接 Render 只读接口：

| 维度 | Render 实际只读记录 | 候选仓库 / #167 |
| --- | --- | --- |
| live commit | `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`，2026-10-23，API 部署 | 审核 HEAD `efc4bd3108ee9e3a72ff181b387ec4df63344c78`，**未部署** |
| Git 分支 | `feat/render-telegram-deployment` | #167 `test/memory-oidc-pinned-origin-real-browser-offline-20261028` |
| 启动入口 | `python scripts/render_start_p3.py` | 仓库 `render.yaml`: `python scripts/render_start.py` |
| 自动部署 | `autoDeployTrigger=off` | Blueprint `autoDeploy=false` |
| 公网 IPv4 规则 | `0.0.0.0/0` | 公网边缘可达，不代表应用原始端口可从公网直接访问 |
| 默认 Render 域名 | `renderSubdomainPolicy=enabled` | 若将来出现自定义域名，必须单独固定 OIDC 公网 Host / redirect URI，不能把别名自行当作合法回调 |
| 健康检查 | `/healthz` | Blueprint 同为 `/healthz` |
| 套餐 / 区域 | `starter`、Oregon | 平台 IP allowlist 的 Web service 设置根据 Render 文档需要 Scale / Enterprise 工作区，不应随意假定可用 |

此次没有读取秘密环境变量值、真实用户数据、日志内容，也没有访问生产回调路径、改动服务、合并或触发部署。**已取得 Render 服务配置和部署元数据不等于真实单请求代理来源证明。** 同样，仓库 Blueprint 与 Render 记录不一致，不说明正在运行的 Telegram 业务必然不安全；它意味着不能把未合并安全 Draft 当作现网安全状态。

参考 Render 官方文件：
- https://render.com/docs/web-services — Render 负载均衡器终止 HTTPS，然后经 HTTP 转发服务；不代表 ASGI `scope.scheme` 为浏览器 HTTPS。
- https://render.com/docs/tls — Render 为 `onrender.com` 和已接入的自定义域名管理证书、自动 HTTP → HTTPS。
- https://render.com/docs/custom-domains — 默认 `onrender.com` 域名可能与自定义域名同时存在；禁止仅根据请求 Host 扩大 OAuth 回调白名单。
- https://render.com/docs/inbound-ip-rules — 公网 IP 规则不等于源站 socket 证明，Web service 的限制功能需要满足工作区套餐；调整需评估 Telegram webhook。

## 新增纯离线发布证据核对

新建 `backend.memory_oidc_render_release_evidence_offline.assess_render_release_evidence_offline`，严格字段白名单的可选脱敏记录，和 `scripts/memory_oidc_render_release_evidence_check_offline.py`。其目的仅是对**仓库 Blueprint 和手工提供的非秘密元数据**做归档级差异检测：

- 默认审核开关 `MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED` 为 OFF。
- 只允许 `service_name`、`repository`、`branch`、`live_sha`、`start_command`、`auto_deploy_trigger`、`render_subdomain_policy`、`ingress_ipv4_cidrs`、`health_check_path`、`deploy_status` 这十个预定义字段。
- 拒绝额外的 token、`envVars`、原始头、所谓 `raw_socket_peer_proof` 或 `edge_tls_proof` 字段、错误 SHA、恶意 CIDR、重复地址和控制字符。对照仓库当前 Blueprint 验证分支、启动入口、自动部署；对比待审核提交与所谓现网提交。
- **人工/CI 输入的任何 JSON 都不是经过 Render 身份认证的来源证明；即使全部字段完全一致，也保持未验证状态。** 生产真实 Cloudflare/Render TLS 与逐请求代理对端、边缘原始头覆盖、固定 Host / 回调域名、真实 IdP/同意、记忆写入等阻断永不被此工具解除。
- 返回结果永远 `may_merge=false`、`may_deploy=false`、`may_enable_oidc=false`、`may_write_memory=false`；CLI 只打印不含原文凭据的归类结果，**退出码固定为 2**。该命令不是 CI 的上线成功判据。

在仓库根目录执行（不读取 Render API、不使用密钥）：

```bash
MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED=true python scripts/memory_oidc_render_release_evidence_check_offline.py \
  --review-branch test/memory-oidc-pinned-origin-real-browser-offline-20261028 \
  --review-sha efc4bd3108ee9e3a72ff181b387ec4df63344c78
```

无 `--observed` 时只使用 Blueprint，报告含 `live_metadata_record_missing`；手动提供红acted `--observed` 只能协助比较，**不能**证明它是 Render 直接读取的数据。Windows/Linux 分别运行 `backend.tests._memory_oidc_render_release_evidence_worker`，覆盖真实已知字段、篡改、缺失、错误值和命令行无密钥输出。

## 尚缺的不可替代生产证据

1. Render/Cloudflare 边缘如何**按请求**绑定真实 TLS 入口、内部转发来源与原始请求，并对客户端自带 `Forwarded` / `X-Forwarded-*` 做何种清洗或覆盖。需要可审计的平台配置或可信边界设计，不能用 `Rndr-Id`、`CF-Ray`、`scope.client` 或 `lambda: True` 代替。
2. 固定唯一外部 HTTPS Host 与实际域名、别名、重定向 URI、浏览器 `__Host-` Cookie / CSRF / Origin 行为，以及生产环境使用的实际入口和安全 HEAD。#167 的本地浏览器双引擎成功只支持设计可行性。
3. 真正的 OIDC/IdP/JWKS、授权码 + PKCE、身份会话撤销和真人同意，及严格分离的记忆写入权限。当前审核器不签发身份，所有功能仍 OFF。
4. 确认将来发布的候选 SHA、Render 真实启动配置、反向代理配置与全部冻结验收相容。**当前仍未达到允许合并或部署安全链的条件。**

本阶段只增加审核器、CLI、测试、CI、文档；不修改 `render.yaml`、Render 服务、生产 OIDC 代码或凭据。所有安全 PR 保持 Draft、未合并、未部署，v48 正式 36/36、108/108 验收门槛保持冻结。
