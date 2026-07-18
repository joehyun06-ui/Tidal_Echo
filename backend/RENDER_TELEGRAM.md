# Render：Telegram 私聊文字 MVP

本方案只部署 allowlist 内的一对一 Telegram 纯文字被动回复。群聊、媒体、命令、主动消息和
自动模型回退均不启用。不要把真实 Token、API Key 或 Secret 写入仓库、命令行或 Codex 对话。

## 为什么是一个服务

relay、内嵌的 `TelegramWorker` 和 `api_loop` 都依赖同一个 SQLite 文件。Render 持久盘只能挂到
一个服务实例，因此 Blueprint 使用一个付费 Web Service、一块 `/var/data` 持久盘和一个实例。
`scripts/render_start.py` 在同一实例中监督两个 Python 子进程：

- relay 绑定 `0.0.0.0:$PORT`，Uvicorn 固定 `--workers 1`；多 worker 会各自启动 TelegramWorker，
  并破坏进程内 SSE 状态假设。
- api_loop 只绑定 `127.0.0.1:3020`。它没有公共管理接口鉴权，不得暴露为第二个 Web Service。

免费 Web Service 会休眠、不能挂持久盘，SQLite 会在休眠或重启后丢失，因此不可用于该 MVP。

## 创建服务前

先把 `feat/render-telegram-deployment` 推送到你自己的 fork；分支尚未推送时不能创建有效部署。
在 Render 创建 Blueprint 时，部署源必须选择 `joehyun06-ui/Tidal_Echo`，分支必须选择
`feat/render-telegram-deployment`，不要选择原作者仓库。`render.yaml` 已设置 `autoDeploy: false`，
以后每次发布都需要在 Render Dashboard 中人工触发。

Blueprint 使用 Render 当前支持的完整版本变量
`PYTHON_VERSION=3.12.11`，而不是依赖会变化的平台默认版本。Blueprint 中所有真实凭据均为
`sync: false`，首次创建时只在 Render Dashboard 中人工填写。版本选择依据是 Render 官方
[Python version](https://render.com/docs/python-version) 与
[language support](https://render.com/docs/language-support) 文档；以后升级 patch 版本前应重新跑完整测试。

必须人工取得或确认：

- BotFather 签发的 `TELEGRAM_BOT_TOKEN`
- 稳定的 `TELEGRAM_BOT_ACCOUNT_ID`
- 唯一允许的数字 `TELEGRAM_ALLOWED_USER_IDS` 和 `TELEGRAM_ALLOWED_CHAT_IDS`
- 模型供应商给出的 `LLM_API_BASE`、`LLM_API_KEY` 和 `LLM_MODEL`

在自己的安全终端分别执行三次以下命令，为 `RELAY_SECRET`、`TELEGRAM_WEBHOOK_SECRET` 和
`CHANNEL_AUDIT_HMAC_SECRET` 生成三个不同值；直接填入 Render，不要粘贴到工单、Git 或聊天：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

启动器会拒绝缺项、重复 Secret、非法 allowlist、持久路径越界、测试 Telegram API、非法超时、
非 `loop` brain target，以及 `_2` 至 `_4` 或 `LOOP_CONFIG` 中的多模型链。

## 启动与健康检查

Render 执行：

```text
Build: python -m pip install -r backend/requirements.txt
Start: python scripts/render_start.py
Health check: /healthz
```

Render 当前的 Blueprint 校验不允许挂载持久盘的服务显式设置 `maxShutdownDelaySeconds`，因此本模板
依赖 Render 默认的关闭等待时间。应用内部 supervisor 仍使用
`SUPERVISOR_SHUTDOWN_GRACE_SECONDS=10` 完成 graceful shutdown，并在超时后清理子进程树。

`/healthz` 是轻量 liveness，不连接 Telegram、模型或 api_loop。`/readyz` 是内部状态 readiness：
它检查 SQLite 只读查询、持久路径、loop brain target、Telegram 配置、worker task 和本机 api_loop。
api_loop 身份通过 supervisor 每次启动生成的临时 nonce 验证，而不是只检查端口。未就绪时返回
HTTP 503；检查不会调用 Telegram 或模型 API。Render 模式下 worker task 若意外终止，relay 会请求
自身退出，由 supervisor 和 Render 完成整体重启；正常关闭不会触发该自愈。公共 API docs/OpenAPI
在此模式下关闭。

Render 直接暴露应用根路由，不使用 nginx `/relay` 前缀。Webhook 地址为：

```text
https://<service>.onrender.com/integrations/telegram/webhook
```

确认 `/healthz` 为 200 且 `/readyz` 返回 `ready=true` 后，才设置 webhook。不要把真实 Token 或
secret 替换进 curl 命令；那会把它们放入 shell history 和进程参数。仓库提供交互式辅助脚本，
通过隐藏输入读取两项 Secret，不打印它们：

```text
python scripts/configure_telegram_webhook.py
```

Windows PowerShell 与 bash 使用同一条启动命令；脚本会依次询问公开 HTTPS webhook URL、Bot Token
和 webhook secret。只在你自己的受信任终端运行，不要把输入复制到 Codex、工单或聊天。
Webhook 与 `getUpdates` consumer 不得同时运行。

## 最小验收

1. `/healthz` 返回 200；`/readyz` 返回 200、`ready=true`。
2. 从 allowlist 中的私人账号发送一条不以 `/` 开头的纯文字消息。
3. 确认只产生一条模型回复和一条 Telegram 回复。
4. 从非 allowlist 账号、群聊或媒体消息验证其被安全忽略，且不会调用模型。
5. 重启服务后再次确认 `/readyz` 和历史数据仍在 `/var/data/relay.db`。

## 回滚

1. 先通过 BotFather 或安全终端删除/切换 webhook，阻止新消息进入待回滚版本。
2. 在 Render Events 中回滚到最近的已验证版本；不要删除或重新创建持久盘。
3. 只有当 relay 与 api_loop 来自同一兼容 revision、`/readyz` 恢复后才重新设置 webhook。
4. 若出现 `dispatch_uncertain` 或 `delivery_uncertain`，先人工检查，不要盲目重试模型或发送。

持久盘服务部署会有短暂停机。备份 SQLite 时应生成一致性备份，不要在写入期间复制半写文件。
