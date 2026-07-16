# Telegram 私聊文字 MVP

本集成把 Telegram 私聊文字持久入队，后台 worker 将消息交给现有 `loop`，并把明确关联的最终
回复通过 Bot API `sendMessage` 以纯文本发回。Webhook 不等待模型生成。

## 人工准备

1. 在 BotFather 人工创建 Bot。开发和生产必须使用不同 Bot。
2. 生成独立、高熵的 webhook secret。
3. 只在服务器环境文件中设置 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_WEBHOOK_SECRET`；文件权限建议
   为 `0600`。不要把值写进 nginx、仓库、前端或 webhook URL。
4. 填写严格的数字 allowlist。MVP 不支持 `*` 或“允许所有人”。
5. 将 relay 的 `brain_target` 设置为 `loop` 并确认 loop 服务可用。

环境变量：

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=replace-with-server-secret
TELEGRAM_WEBHOOK_SECRET=replace-with-random-server-secret
TELEGRAM_BOT_ACCOUNT_ID=replace-with-stable-bot-account-id
TELEGRAM_ALLOWED_USER_IDS=100000001
TELEGRAM_ALLOWED_CHAT_IDS=100000001
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_TEXT_LENGTH=4096
TELEGRAM_WORKER_POLL_SECONDS=1
```

### Reliability and deployment contract

- Set `CHANNEL_AUDIT_HMAC_SECRET` to a separate, high-entropy, server-only value. Audit identifiers are truncated HMAC-SHA256 values; raw Telegram user/chat IDs and the HMAC secret are never logged.
- `LOOP_MODEL_TOTAL_TIMEOUT_SECONDS` is one wall-clock deadline for the entire model route chain, including every permitted fallback and all streaming reads. Keep `LOOP_MODEL_TOTAL_TIMEOUT_SECONDS=120`, `LOOP_CALLBACK_TIMEOUT_SECONDS=30`, `LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS=15`, and `LOOP_DISPATCH_TIMEOUT_SECONDS=180`, or preserve `dispatch >= model total + callback + margin`. Relay and `examples/api_loop.py` must be upgraded and restarted together before Telegram is enabled.
- Model fallback is allowed only after an explicit provider rejection proving that generation never started (currently recognized model-not-found/unsupported codes). Timeout, disconnect, response loss, malformed/incomplete streams, any failure after a delta, and generic exceptions never fallback. This applies to Telegram and ordinary Web loop requests.
- Production accepts `https://api.telegram.org` by default. A self-hosted HTTPS Bot API endpoint must be explicitly listed in `TELEGRAM_API_BASE_ALLOWLIST`; unsafe URL forms are rejected. `TELEGRAM_TEST_MODE=true` is for isolated tests only.
- Authenticated permanent policy rejects return `200` with `ignored=true`. Bad webhook authentication remains `401`; malformed/oversized bodies use fixed safe errors; only temporary database/service failures return retryable non-2xx responses.
- Generation is persisted as `dispatching` before the loop call. `awaiting_reply`, `dispatch_uncertain`, and `delivery_uncertain` are never automatically redispatched. Uncertain work may require manual inspection to avoid duplicate model charges or sends.
- A legacy/mismatched loop acknowledgement without callback or correlation fields is recorded as `dispatch_uncertain` with `correlation_missing`; it is not failed or retried. An old callback may still appear in Web history but never guesses a Telegram destination.
- Final reply history, job completion, completion identity, delivery, and all `delivery_parts` commit in one SQLite transaction. Each delivered part and Telegram message ID commits immediately; a part left `sending` after restart becomes `delivery_uncertain`.
- `TELEGRAM_GENERATION_MAX_ATTEMPTS` defaults to `2`; worker polling is clamped to at least `0.25` seconds. SQLite remains single-instance only.
- Backups must include `schema_migrations`, messages, mappings, inbound/external messages, jobs, `telegram_completions`, deliveries, `delivery_parts`, rate-limit/audit tables, and push subscriptions.
- Put secrets in a mode-`0600` systemd `EnvironmentFile` or the platform secret store. Never place them in unit command lines, public shell history, URLs, Git, or frontend files.
- Tests use in-memory ASGI transports and explicitly block all real sockets, including loopback. Production systemd must run one relay/worker instance.

Telegram `429` records `retry_after` and stops as a finite failed delivery in this MVP. Telegram `5xx`, timeout, connection loss, invalid JSON, or missing `message_id` are conservatively `delivery_uncertain` and are not resent automatically.

配置缺项、allowlist 非整数/含通配符时，集成会安全地保持禁用，不影响原 relay。
`TELEGRAM_API_BASE` 仅用于将测试指向 Mock server；生产保持官方 Bot API 基址。

## Webhook

公开 URL 为：

```text
https://your-domain.example/relay/integrations/telegram/webhook
```

使用 Telegram `setWebhook` 时，同时在请求体提供：

```json
{
  "url": "https://your-domain.example/relay/integrations/telegram/webhook",
  "secret_token": "replace-with-the-same-server-secret",
  "allowed_updates": ["message"]
}
```

不要把 Bot token 拼进文档、日志或可共享的 shell 历史。可在本地安全终端中从环境变量构造
Bot API 请求。Telegram webhook 和 `getUpdates` 不能同时消费同一个 Bot；启用 webhook 前先
停止 polling consumer。

Telegram 会在 `X-Telegram-Bot-Api-Secret-Token` 请求头发送 secret。relay 使用定时安全比较，
拒绝缺失/错误 secret。仅接受 allowlist 内用户与 chat 的非空私聊纯文字；群聊、频道、编辑消息、
附件、图片、语音、sticker、命令、inline query 和 callback query 均拒绝或安全忽略。

## 持久化、恢复与限制

`update_id`、外部消息 ID、conversation mapping、generation job 和 delivery outbox 均有数据库
唯一约束。相同 Bot account + chat 永久映射到随机 `api_session`，不暴露 chat ID，也不复用
Web 的 `local_only`/恢复会话。进程重启后 worker 会回收 lease 过期的 generation job；已
`delivered` 或 `delivery_uncertain` 的 delivery 不会自动重发。

发送超时、连接中断或响应不确定会标记 `delivery_uncertain`，避免盲目重复发送；明确 4xx 会
标记 `failed`。长回复按 4096 字符、优先换行安全分段，不启用 Markdown/HTML。

当前只支持：单实例 SQLite、Telegram 私聊文字、被动回复和 `loop`。不支持附件、语音、群聊、
主动消息、Claude desktop channel、设备工具或 Operit。多实例部署前必须迁移到支持并发锁和
可靠队列的数据库/任务系统。数据库备份必须包含 `messages`、全部 `channel_*` 表、
`inbound_events`、`external_messages`、`generation_jobs`、`delivery_attempts` 和
`schema_migrations`。
