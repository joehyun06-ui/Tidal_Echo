# 归汀 · 合并后的 P3 发布分支与 Render Blueprint 对齐（Draft / OFF）

## 触发原因：2026-10-28 的并行合并

仓库的 **PR #169 已正式合并**，merge commit `33f2fb9e09d42a901728355720b8334054c7fab5`，base `feat/render-telegram-deployment`。它集成了此前离线隔离的用量账本与大部分记忆/OIDC 审核代码（包括 #157 的 Uvicorn `--no-proxy-headers`、#158 的 OIDC 默认禁止和 #161 的 P3 入口保护），但没有启用真人 OIDC 登录或记忆写入。因此“全部安全 PR 仍未合并”的历史表述**已经过时**。尚未合并的 #168 是单独的发布元数据证据核对器。

用户已授权对 Render `ouott` 工作区 / `ouou-home-telegram` 服务进行只读检查。本轮初次核对时 live 版本仍是 **2026-10-23 的 `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`**；**之后的独立只读复核发现状态已更新**：2026-10-28 14:49 UTC，Render 通过 API 触发对 #169 合并提交 `33f2fb9e09d42a901728355720b8334054c7fab5` 的部署，14:51 UTC 完成，状态 `live`，原 `a2d8699a` 为 `deactivated`。实际启动命令依旧为 `python scripts/render_start_p3.py`，分支 `feat/render-telegram-deployment`，`autoDeployTrigger=off`、默认 `onrender.com` 域名启用，公网 IP 白名单 `0.0.0.0/0`。**已经上线的是 #169 中已合并的代码，而不是本 PR #170 的 Blueprint 修订。** 本阶段所有操作仍是 GitHub Draft 和 Render 只读查询，没有自行触发部署。

## 本阶段限定改动

针对真实的 P3 入口，将仓库 Blueprint `render.yaml` 修正为：

- `startCommand: python scripts/render_start_p3.py` —— 与只读 Render 服务元数据一致，不再指向此前的基础 `render_start.py`。
- `autoDeployTrigger: off` —— 明确使用 Render 当前首选字段，保留原有 `autoDeploy: false` 作为冗余兼容值；两者值均表示不自动部署，且当前字段优先。
- 保持 `branch: feat/render-telegram-deployment`、`healthCheckPath: /healthz`、Starter 套餐、数据盘、原有 Telegram/Web-provider 配置及所有已存在的密钥占位不变；**不添加或打开 OIDC、记忆写入、付费 API 开关**。
- 更新旧的 `backend.memory_oidc_render_drift_review` 期望的 Blueprint 命令及相关测试，避免自动化仍将旧基础入口当作上线目标。
- 增加 `backend.tests._render_p3_blueprint_parity_offline_worker`：在无网络情况下通过真实 `render_start_p3.child_commands` 检查普通/Codex 双入口、唯一 `--no-proxy-headers`、固定双监听地址、OIDC 审核误开时的提前中止、自动部署关闭、密钥占位不被提升，以及 Blueprint 更改时的漂移阻断。Windows/Linux 双平台 CI。

**本 PR 只提交 Draft 候选，不修改 Render 本身、不执行 Blueprint sync，也不触发部署。**

## 上线前的两项不同审批

1. **业务代码候选部署**：即使只想让已合并的 P3/用量账本改进上线，也须先明确部署 commit 与现有 Telegram webhook/数据库持久卷兼容，记录备份和回滚方案；对服务实际 `startCommand`、`autoDeployTrigger`、存储挂载重新只读确认，再在用户明确批准后通过独立发布流程操作。**当前无此批准**。
2. **真实浏览器身份 / OIDC / 记忆写入**：属于另一项更高门槛的功能上线。即使第一项顺利，也**不能**启用这些开关。Render/Cloudflare 的逐请求来源、可信转发头清洗、固定公网 Host/Origin/TLS 及 IdP/JWKS/PKCE、真实浏览器 Cookie、真人同意和记忆授权，仍未取得真实生产证据。#165 发现的外部 HTTPS 到内部 HTTP 兼容性问题，只通过 #166/#167 的合成边缘证明完成本地可行性验证，绝不等于生产可信代理已建立。

## 关于默认域名

Render 官方确认每个 Web 服务自带 `onrender.com` 域名；只有在服务存在至少一个已添加的自定义域名后，才能禁用默认子域名。当前没有独立验证真实自定义域名的存在或完整 TLS/redirect 策略，**本 PR 不更改 `renderSubdomainPolicy`**，避免将现有服务突然切断。

参考来源：
- https://render.com/docs/blueprint-spec
- https://render.com/docs/web-services
- https://render.com/docs/custom-domains

冻结 v48 正式验收 **36/36、108/108** 不变，真实 OIDC、记忆写入、付费 API 保持关闭。
