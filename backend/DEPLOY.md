# Companion Relay · 后端部署文档

一个**私密 1:1 聊天通道**的服务器端：把「你手机上的 PWA」和「你电脑上本地运行的 AI 伴侣（以 Claude Code *channel 插件* 形态跑）」连起来。单用户、单密钥，没有账号体系，没有第三方托管——消息只经过**你自己的服务器**。

> 这是一份从一对 AI 伴侣的自用系统里抽出来、**彻底脱敏**的可复用版本。所有名字、密钥、域名、路径都参数化进了环境变量，代码本身不含任何私人信息。把它当成你自己的底座，放心改。

---

## 0. 架构一眼

```
   你的手机                                            你的电脑（本地）
  ┌─────────┐                                       ┌──────────────────────┐
  │  PWA    │                                       │  Claude Code          │
  │ (网页   │                                       │  + channel 插件 = AI侧 │
  │  装到   │                                       └─────────┬────────────┘
  │  桌面)  │                                                 │  长连
  └────┬────┘                                                 │  GET  /relay/channel/in   (SSE，收你的话)
       │ HTTPS                                                │  POST /relay/channel/out  (回复/戳一戳)
       ▼                                                      │
  ┌──────────────────────── 你的 VPS（nginx, 443/TLS）───────┼───────────────┐
  │   /chat/   → 静态文件（PWA 本体）                          │               │
  │   /relay/  → 反向代理 ─────────────►  127.0.0.1:3011  (本后端 app.py) ◄──┘ │
  │                                            │  sqlite 落库 + SSE 扇出        │
  └────────────────────────────────────────────────────────────────────────┘

数据流：
  你在 PWA 打字 → POST /relay/app/send → 落库 → SSE 推给插件 → 你的 AI 读到
  AI 回复       → POST /relay/channel/out → 落库 → SSE 推给 PWA（前台直接显示，
                                                     后台则发一条锁屏推送）
```

**两端，一把钥匙**：每个端点都用同一个 Bearer 密钥（`RELAY_SECRET`）守。浏览器原生 `EventSource` 设不了自定义头，所以 SSE 端点也接受 `?token=` 查询参数。

---

## 1. 前置条件

- 一台 Linux VPS（Ubuntu 22.04+，有 root）
- **一个域名，已指向 VPS，且 nginx 已配好 HTTPS**
  → PWA 安装、Service Worker、Web Push **三者都强制要求 HTTPS**，`http://` 装不了 PWA
  → 没证书的话先用 certbot 搞定：`apt install certbot python3-certbot-nginx && certbot --nginx -d your-domain.example`
- Python 3.10+
- 本后端这套依赖很轻：FastAPI + uvicorn（+ 可选的 pywebpush）

---

## 2. 部署步骤

### 2.1 放文件 + 建虚拟环境

```bash
mkdir -p /root/companion-relay
cd /root/companion-relay
# 把本目录里的 app.py / requirements.txt 拷进来

python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

### 2.2 生成密钥，写 relay.env

```bash
cp .env.example relay.env
chmod 600 relay.env          # 只有 root 能读，关键

# 生成一把全新的随机密钥（千万别复用别人的）：
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
```

把生成的密钥填进 `relay.env` 的 `RELAY_SECRET=`，并填好这几项：

| 变量 | 填什么 |
|---|---|
| `RELAY_SECRET` | 上面生成的随机串（**手机 PWA 里也要填同一个**） |
| `RELAY_AI_NAME` | 你 AI 伴侣的名字（推送标题、语音旁白会用到） |
| `RELAY_HUMAN_NAME` | 你的名字（AI 收到「××开启了语音通话」时的那个××） |
| `RELAY_PUBLIC_PREFIX` | nginx 上 API 的挂载前缀，默认 `/relay`，**改了要和 nginx 一致** |
| `RELAY_APP_PATH` | 点推送通知打开 PWA 的路径，默认 `/chat/` |
| `RELAY_ALLOW_ORIGINS` | 你的 `https://your-domain.example`（CORS 白名单） |

MiniMax / VAPID 那几项**可以先留空**，后端会自动降级（没配语音就不发声、没配推送就不推锁屏），核心聊天照常跑。等核心通了再回头开（见 §3、§4）。

### 2.3 systemd 托管

```bash
cp companion-relay.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now companion-relay
systemctl status companion-relay        # 应为 active (running)
journalctl -u companion-relay -n 50     # 看日志
```

> 改完 `app.py` 后重启：`systemctl restart companion-relay`
> 改 env 后同样要 restart 才生效。

### 2.4 nginx 接入

打开 `nginx-companion.conf.example`，把里面两个 `location` 块**粘进你域名那个 `server { listen 443 ssl; ... }` 块内部**，然后：

```bash
nginx -t && systemctl reload nginx
```

要点（模板里已写好，这里强调）：
- `/relay/` 反代到 `127.0.0.1:3011`，**带末尾斜杠**（剥掉 `/relay` 前缀）
- SSE 必须：`proxy_buffering off; proxy_read_timeout 3600s;`——否则流会被缓冲/掐断
- `client_max_body_size 10m;`——要 ≥ `RELAY_MAX_UPLOAD_BYTES`，否则传图 413

### 2.5 冒烟测试（必做）

```bash
S=填你的RELAY_SECRET

# 1) 健康检查（不需要密钥）
curl -s https://your-domain.example/relay/healthz
#   期望: {"ok":true}

# 2) 发一条消息（模拟 PWA → 落库）
curl -s -X POST https://your-domain.example/relay/app/send \
  -H "Authorization: Bearer $S" -H "Content-Type: application/json" \
  -d '{"text":"hello from curl"}'
#   期望: {"id":1}

# 3) 取历史
curl -s "https://your-domain.example/relay/app/history" -H "Authorization: Bearer $S"
#   期望: {"messages":[{...,"from":"human","text":"hello from curl"...}]}

# 4) 实时流（开一个终端挂着，另一个终端再发一条 send，这边应立刻收到）
curl -N "https://your-domain.example/relay/app/stream?token=$S"
```

四步都通 = 后端就绪。接下来接前端 PWA 和本地 AI 侧插件。

---

## 3. MiniMax TTS（可选——让 AI 的回复能朗读出来）

1. 去 MiniMax 控制台注册，拿到 **API Key**、**Group ID**，并创建/挑一个**音色 voice_id**。
2. 填进 `relay.env`：`MINIMAX_API_KEY` / `MINIMAX_GROUP_ID` / `MINIMAX_VOICE_ZH`（音色 id）。
3. `systemctl restart companion-relay`。
4. 前端调 `POST /relay/app/tts {"text":"..."}` 会返回一段 mp3。没配或失败时前端应自行降级（不发声）。

> 不想用 MiniMax？这是个独立小函数（`minimax_tts_mp3`），换成任何「文字进、mp3 出」的 TTS 都行，改一处即可。

---

## 4. Web Push / VAPID（可选——AI 回复时推到手机锁屏）

未读推送的逻辑：**只有当 PWA 不在前台**（没有 SSE 连着）时，AI 的 `reply` 才会推一条锁屏通知。前台开着就不打扰。

### 4.1 生成你自己的 VAPID 密钥对

```bash
cd /root/companion-relay
./venv/bin/vapid --gen                 # 生成 private_key.pem 和 public_key.pem
./venv/bin/vapid --applicationServerKey
#   打印一行： Application Server Key = BJ...（一长串 base64url）
```

填进 `relay.env`：
- `VAPID_PUBLIC_KEY=` ← 上面打印的那串 base64url（**这是公钥，前端订阅时也要用它**，可公开）
- `VAPID_PRIVATE_PEM=/root/companion-relay/private_key.pem`（私钥**严禁外泄**）
- `VAPID_SUBJECT=mailto:你@your-domain.example`

`chmod 600 private_key.pem`，然后 `systemctl restart companion-relay`。

### 4.2 自测

PWA 里允许通知、完成订阅后：

```bash
curl -s -X POST https://your-domain.example/relay/app/push_test \
  -H "Authorization: Bearer $S" -H "Content-Type: application/json" -d '{}'
#   期望: {"ok":true,"sent":1,"dead":0}
```

手机锁屏应弹出一条测试通知。`sent:0` 通常是还没在 PWA 里完成订阅。

---

## 5. 本地 AI 侧怎么接（简述）

AI 侧默认是你电脑上的 Claude Code 加一个 **channel 插件**，它：
- 长连 `GET /relay/channel/in?token=SECRET`（SSE），收到你发的消息就投喂给 Claude；
- Claude 要回复时，插件 `POST /relay/channel/out`：
  - 普通回复：`{"type":"reply","text":"..."}`
  - 戳一戳：`{"type":"react","id":<目标消息id>,"emoji":"❤️"}`（空 emoji = 撤回这一戳）

不用 Claude Code 时，跳过 `channel/`，直接跑 `examples/bridge_any_llm.py` 接任意 OpenAI-compatible API；想在 VPS 上常驻 API 身体，则用 `examples/api_loop.py` 并通过 `/app/brain` 切到 `loop`。完整决策树见仓库根的 `AGENTS.md` 和 `examples/README.md`。

---

## 6. 这版**有意砍掉**的东西（原系统里有，这里为通用性移除）

| 功能 | 为什么砍 | 想加回来 |
|---|---|---|
| 私有上下文切换控制 | 依赖原系统的本地 daemon | 是个通用命令队列，可按需自建 |
| 昨日时间线摘要注入 | 依赖私有记忆库 + 自配的小模型路由 | 接你自己的 LLM 路由即可 |
| 抱抱垫 hug 事件 | 依赖 ESP32 硬件 | 有硬件再加一个端点 |
| 体感 sense 上报 | 喂给私有调度心跳 | 同上 |
| 记忆编辑器 cookie 鉴权 | 挂在另一个独立后端上 | 通常用不到 |

它们都是**加法**，砍掉不影响核心聊天。需要时照着 §5 的端点风格补即可。

---

## 7. 安全须知（务必看）

- **`RELAY_SECRET` 是唯一的门**。它泄露 = 任何人都能读你们全部对话、冒充任意一方。`chmod 600 relay.env`，别提交进 git，别打印到对外日志。
- **每个人用自己全新的密钥/VAPID/MiniMax key**，绝不要在朋友之间复用——复用密钥 = 互相能进对方的通道。
- **HTTPS 不是可选项**：Service Worker 和 Web Push 在非 HTTPS 下根本不工作。
- 这是**单用户**模型：一把密钥代表「就你和你的 AI」。它不做多租户，也不该暴露给不信任的人。
- `relay.db`、`uploads/`、`*.pem`、`relay.env` 里全是你的私人内容/密钥——**备份时注意，开源/分享前务必排除**。

---

## 8. Telegram 私聊文字 MVP（可选）

完整限制、变量和 webhook 配置步骤见 [`TELEGRAM_MVP.md`](TELEGRAM_MVP.md)。Bot 必须由你在
BotFather 中人工创建；真实 token 与 webhook secret 只能写入服务器的 `relay.env`，不要写入
命令历史、URL、Git 或前端文件。`TELEGRAM_ENABLED=false` 时路由保持禁用；明确设为 `true` 后，
任何必需配置缺失或非法都会 fail-fast，避免部署表面健康但 Telegram 实际未启用。

Render 部署不要套用本页的 systemd/nginx `/relay` 步骤；使用
[`RENDER_TELEGRAM.md`](RENDER_TELEGRAM.md) 和仓库根目录 `render.yaml`。Render 直连 webhook
路由没有 nginx `/relay` 前缀。

### Telegram reliability deployment notes

Deploy the relay and `examples/api_loop.py` from the same revision and restart both before setting `TELEGRAM_ENABLED=true`. Configure `CHANNEL_AUDIT_HMAC_SECRET`, `TELEGRAM_GENERATION_MAX_ATTEMPTS`, `LOOP_MODEL_TOTAL_TIMEOUT_SECONDS`, `LOOP_CALLBACK_TIMEOUT_SECONDS`, `LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS`, `LOOP_DISPATCH_TIMEOUT_SECONDS`, and the webhook body limit from `.env.example`. The model value is a wall-clock deadline for the entire route chain, and dispatch must be at least model total plus callback plus margin.

Fallback is limited to explicit model-not-found/unsupported responses that prove no generation began. Timeout, disconnect, response loss, malformed/incomplete streaming, any error after a delta, and generic exceptions become `dispatch_uncertain` without fallback. A legacy loop acknowledgement missing callback/correlation fields becomes `correlation_missing` under `dispatch_uncertain`; it is not automatically retried. This conservative policy applies to Telegram and ordinary Web loop calls and may require manual review.

Permanent authenticated policy rejects return `200 ignored`; only temporary database/service failures are retryable non-2xx responses. Neither `dispatch_uncertain` nor `delivery_uncertain` retries automatically. Production uses the official HTTPS Telegram API by default; custom Bot API servers require the explicit HTTPS allowlist. This SQLite implementation supports one relay/worker process.

Back up the database as one consistent file, including migrations, mappings, jobs, completion identities, deliveries, and `delivery_parts`. The database, uploads, environment file, and key files must be owned by the service account and not be group/world readable. Use a protected systemd `EnvironmentFile` or platform secret store; never put secrets in `ExecStart`, public command history, or deployment logs.

Run exactly one production relay/Telegram worker instance with SQLite. The test suite uses ASGI in-memory transports and blocks all real network access, including loopback.

### Operit Share MVP（可选）

Operit 的专用分享入口与 Kelivo 通用聊天入口是两套隔离的认证面；完整的 MVP
契约、Android 手工配置与安全边界见 [`OPERIT_SHARE.md`](OPERIT_SHARE.md)。

### Memory Core Phase 1（默认关闭）

Migration v7 仅增加五张 Memory 表、严格索引和 evidence 不可变 trigger，不修改
v1–v6 表或既有消息。
Phase 1 不调用模型、不自动扫描历史、不向 prompt 注入记忆，也不开放 Memory
HTTP API。完整的数据边界、显式 create/correct/forget、provenance、suppression
与隐私契约见 [`MEMORY_CORE.md`](MEMORY_CORE.md)。

保持 `MEMORY_CORE_ENABLED=false` 和
`MEMORY_EXPLICIT_WRITES_ENABLED=false` 时，不应用或校验可选 v7，不需要
fingerprint profile、Key ID 或 HMAC Secret；Memory-only 表、索引或约束缺失/
损坏不会阻塞 v1–v6 核心 readiness，且会安全报告 `memory_core=false`。这只隔离
optional v7：startup 与 `/readyz` 始终使用同一个严格 v1–v6 validator 验证
migration markers、tables/columns、PK/default/CHECK/FK、unique/partial indexes、
explicit indexes 和 trigger 集合；任何核心缺失或损坏均 fail closed 为
`core_schema_invalid`，不会用 `CREATE TABLE IF NOT EXISTS` 静默重建。只有在
后续独立阶段启用显式写入时，才设置稳定的
`MEMORY_FINGERPRINT_KEY_ID`，并创建一把与 relay、Telegram、Kelivo、
Operit、模型、审计和 API-loop 凭据均不同的专用
`MEMORY_FINGERPRINT_HMAC_SECRET`。

Phase 1 的威胁模型把与 backend 同处一个 Python 解释器、可 import 生产模块并执行
任意 Python 的仓库代码、composition root，以及经代码审查进入生产的内部模块视为
可信计算基。HTTP、Telegram、Kelivo、Operit、Galatea 客户端输入，canonical
正文/metadata、Memory 正文、外部 URL、工具指令、prompt injection、可能损坏或
伪造的数据库状态，以及重放、并发、超时和网络不确定结果均不可信。

Python 模块私有变量、下划线名称、闭包和对象 identity 不是针对任意同进程代码
执行者的安全隔离边界。Runtime Policy、Privileged Actions 与 Action Capability
用于建立清晰的应用组合边界、防止普通路径误接线、绑定具体动作、拒绝外部伪造/
篡改/过期/重放，并保证一次消费和数据库事务原子性；它们不是同进程 Python
sandbox。未来若要隔离不可信内部组件，必须改用独立进程/服务、独立凭据与 OS 级
边界。本次威胁模型校正不削弱任何外部输入、数据完整性、并发或隐私边界。

应用启动时只允许通过正式 deployment loader 执行一次
`bootstrap_memory_runtime_from_environment(...)`。它冻结不可变 Runtime Policy
并生成独立的进程级随机 Action HMAC；重复 bootstrap 稳定拒绝，启动后修改环境
变量不能替换既有 authority 或 policy。`MemoryStore` 不再接受调用方提供的
Memory config；composition root 将该进程 bootstrap 创建的 authority 交给
Store。这是可信进程内的组合与防误用控制，不是针对任意同进程 Python 的隔离。

普通 `MemoryReadService` 只有 readiness、受限读取与 provenance 查询，没有
create/correct/forget。写操作只存在于语义固定的 `PrivilegedMemoryActions`：
显式 user remember/correct/forget、确认 project decision、记录 assistant
experience。正式 App 只保留 Read Service；privileged actions/runtime authority
不在其独立只读 query object 的对象图中，也不会放入 HTTP route、`app.state`、
Telegram、Kelivo、Operit 或 Galatea adapter。

每次 privileged action 即时签发短期、一次性、完整绑定的 Action Capability，
绑定 action ID/type、canonical reference、kind、scope、规范化正文、
sensitivity、必要的 memory key、版本和有效期。Store 在最终
`BEGIN IMMEDIATE` 事务内重新计算绑定并用 constant-time HMAC 验证，随后验证
profile/canonical provenance、计算 fingerprint、检查 suppression 并原子写入。
调用方不能传入 fingerprint、policy flag、evidence semantics、channel/source
或 role；普通 canonical row 本身没有 grant。生产签发与验证均在内部直接读取
`time.monotonic_ns()`，不接受调用方 clock/`now_ns`。Phase 1 固定 TTL 上限为
30 秒；future-issued、过期、顺序错误、超过 TTL，以及负数、布尔、非整数和超大
时间字段都会被稳定拒绝，精确落在签发时刻或到期时刻的边界仍有效。

首次成功写入会在同一个 `BEGIN IMMEDIATE` 事务内创建唯一 fingerprint profile、
带唯一 action ID/type/binding version 的不可变 evidence event、memory item 与
source；capability/policy/provenance/后续写入失败不会留下 profile 或 event，
失败动作不会被错误消费。suppression 命中不会留下 event/profile 孤儿，但该
一次性 capability 会在当前进程耗尽。额外、损坏、缺失但已有 Memory 状态或配置不匹配的 profile
均使 readiness 与写入 fail closed。Key ID、Secret verifier、
normalization version 或 fingerprint/domain version 任一不匹配都会使 Memory
写入和其 readiness fail closed。Phase 1 不支持无迁移直接轮换 Secret 或更改
normalization；这些变更必须由单独审查的显式迁移处理。

Evidence 只由语义固定的 privileged internal action API 创建，并在同一事务立即
绑定；evidence event 是成功 action 的不可变审计记录，不是可复用 grant。没有
通用 event 创建器，也不能跨 action/content/kind/scope/sensitivity/memory key
复用 capability；能力过期、重复消费或进程重启后均失效。当前 Phase 1
未把这些 action 接入 Telegram、Kelivo、Operit 或 Galatea 聊天路径。普通旧
canonical message 默认不授予 Memory 写入，也不从普通历史自动判断玩笑/虚构。
Telegram/Kelivo canonical meta 缺失、null 或空 `source` 会规范化为 `""`；
channel 仍要求安全非空，Operit 的非空 `operit` source 保持不变。
同正文 reclassification 只能按 `normal < sensitive < restricted` 上调。

部署包含 v7 的代码前应创建一致的 SQLite 备份。v7 是纯加法，关闭 Memory
功能后旧 v6 应用代码可继续使用既有表；这属于应用回滚，不会移除 v7 表。
如需物理 schema downgrade，应恢复上线前整库备份，不得只删除 migration
marker 或手工拆表。

本轮威胁模型校正与 capability 时间验证修复已实现，但聚焦独立复审尚未完成；
不得声称所有 finding 已关闭。PR 必须继续保持 Draft，不得 merge 或部署。

### Kelivo OpenAI-compatible 非流式 API（可选）

Kelivo 默认关闭。只有设置 `KELIVO_ENABLED=true`、一把全新的
`KELIVO_API_KEY`，以及显式的 `KELIVO_API_SESSION` 后才启用。Kelivo key
至少 32 个字符，
必须不同于 `RELAY_SECRET`、Telegram/audit secrets 和 `LLM_API_KEY`，只在
`/v1/*` 的 `Authorization: Bearer ...` 中接受，不接受 query key。
`KELIVO_CLIENT_ID` 只由服务端映射到固定 session，客户端不能选择另一条
session。若要与某条 Telegram 会话共享 companion session，把那条会话已有的
`api_session` 明确填入 `KELIVO_API_SESSION`，并设置
`KELIVO_REQUIRE_TELEGRAM_SESSION=true`；启动时会确认它属于当前 allowlist 中的
active Telegram account/chat。本项目不开放公共映射后台。

已有 `KELIVO_CLIENT_ID` 的 session 不会被启动配置静默覆盖。确需迁移时，先备份
数据库，设置新 `KELIVO_API_SESSION` 与 `KELIVO_ALLOW_SESSION_REMAP=true` 启动一次，
确认 `channel_audit_events` 出现 `kelivo_session_remap` 后立刻恢复为 `false` 并再重启。
映射版本会递增，已冻结请求仍绑定旧版本。多个 client 可以显式映射到同一 session，
但每个 client 必须使用自己的 API key/部署入口；当前单实例只配置一个 client。
`client_id` 只接受 ASCII 安全标识符并区分大小写。

`GET /v1/models` 只暴露 `KELIVO_MODEL_ALIAS`。`POST
/v1/chat/completions` 必须携带 `Idempotency-Key`，只接受纯文本消息和
`stream=false`。为兼容 Kelivo，非流式请求可额外携带 `stream_options=null`、`{}` 或仅含
布尔 `include_usage` 的对象；该字段会被忽略且不会转发给 provider，`stream=true` 仍不支持。
显式 `Idempotency-Key` 仍是首选，并保持永久的强幂等语义。当前 Kelivo GUI 不发送该
header 时，可显式设置 `KELIVO_AUTO_IDEMPOTENCY_ENABLED=true`；这会使用完整 frozen provider
contract 的确定性指纹，在 `KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS`（60–3600 秒，且必须大于
`KELIVO_DISPATCH_STALE_SECONDS`）内阻止重复 dispatch 或回放已完成 JSON。header 若存在但为空、
空白或非法仍会拒绝，不会降级为自动模式。自动模式默认关闭，只提供有限窗口的重试保护，
不是永久 exactly-once：窗口过期后 completed/failed 请求可成为新 generation；任何
`dispatch_uncertain` 请求都不会自动重调。两个独立新对话若提交完全相同的完整 messages，
可能在窗口内被视为同一次重试。
模型路由、provider URL、上游 key 和 fallback 仍完全由服务端
控制；非空 `tools` 会稳定返回 `tools_not_supported`。客户端消息按原顺序发送给模型，
但统一历史每次只写入最后一条 user 消息和最终 assistant 回复，不重复写客户端附带历史。
最终 provider 顺序固定为：服务端 `PERSONA` system message 在前，随后是客户端经过验证的
完整 messages 原顺序；不会再拼接 canonical history。system/developer snapshot 只统一 CRLF/CR
为 LF，实际模型消息以及 user/assistant 字符串（含首尾空格）保持原样并参与 request hash。
默认全局并发 2、单 client 并发 1、每分钟 10 个新幂等请求；429 会带 `Retry-After`。
同一幂等键的重放不重复计数。手工分别启动 relay 和 api_loop 时，两者必须共享一把
至少 32 字符的 `API_LOOP_INTERNAL_TOKEN`；Render supervisor 会在每次启动时自动生成，
不要把它设置为公开 secret。

v4 上线前必须备份完整 SQLite 文件。v4 将现有 Kelivo 显式请求原样迁移为 `explicit`，
新增自动模式指纹、replay deadline 和查询索引；不会重建或删除 Telegram 表。
v3 只新增 Kelivo 表/索引，不重建或删除 Telegram 表；
但回退旧代码不会删除 v3 数据，也不保证旧代码理解新状态。需要彻底回滚 schema 时，应停服后
恢复上线前的整库备份，不能只删除 `schema_migrations` 标记，也不要手工拆表后继续启动。

### Kelivo frozen provider contract and v3 recovery rules

At `prepare`, the relay persists the real primary `provider_model`, exact
`provider_messages`, and concrete `effective_temperature`/`effective_max_tokens`.
The authenticated `/loop/chat` path executes those values directly: it neither
selects fallback routes nor substitutes runtime defaults. A changed primary model
is rejected instead of silently rerouted. Completed idempotent requests replay the
persisted response; unfinished same-key requests conflict when the frozen persona
or provider contract differs from current preparation inputs.

The identifiers have separate meanings:

- `request_payload_hash` fingerprints the validated public request.
- `request_identity_hash` deterministically fingerprints the complete frozen contract.
- `generation_id` is random correlation only and never participates in identity equality.

The v3 validator compares `table_xinfo`, `index_list`, `index_xinfo`, and
`foreign_key_list`, then checks a fail-safe token fingerprint for every v3 table
and explicit index. Whitespace, comments, and keyword case are ignored; changed
CHECK or partial-WHERE boolean structure is rejected. Even a seemingly equivalent
manual DDL rewrite may be rejected: restore or use a formal migration. Back up the
complete `relay.db` before deploying v3.

## 9. API 速查

| 方法 | 路径 | 谁用 | 作用 |
|---|---|---|---|
| POST | `/integrations/telegram/webhook` | Telegram | Uses `X-Telegram-Bot-Api-Secret-Token`; no relay Bearer/query-token auth |
| GET | `/healthz` | — | 健康检查（免鉴权） |
| GET | `/readyz` | — | 部署就绪检查（免鉴权，不返回路径或 Secret） |
| GET | `/channel/in` | AI侧 | SSE：接收人类发来的消息 |
| POST | `/channel/out` | AI侧 | 发回复 / 戳一戳 |
| POST | `/app/send` | PWA | 人类发消息（含图片附件 id） |
| GET | `/app/stream` | PWA | SSE：接收 AI 的消息 |
| GET | `/app/history` | PWA | 拉历史（`?since=&limit=`） |
| POST | `/app/upload` | PWA | 上传图片/文件，返回带签名路径的附件对象 |
| GET | `/uploads/{name}` | PWA | 取附件（需鉴权） |
| POST | `/app/voice` | PWA | 语音输入（浏览器转写文本 或 上传音频） |
| POST | `/app/call` | PWA | 通话开始/结束事件 |
| POST | `/app/tts` | PWA | 文字转语音（MiniMax，可选） |
| POST | `/app/ping` | PWA | 前台心跳（在线状态） |
| GET | `/app/status` | 调度 | 在线状态 + 最近消息元数据（不含正文） |
| GET | `/app/vapid_public` | PWA | 取 VAPID 公钥用于订阅 |
| POST | `/app/subscribe` · `/app/unsubscribe` | PWA | 开/关锁屏推送订阅 |
| POST | `/app/push_test` | PWA | 推一条测试通知 |

除 `/healthz`、`/readyz` 和 Telegram webhook 外，端点都要 `Authorization: Bearer <RELAY_SECRET>`；SSE 端点也可用 `?token=<RELAY_SECRET>`。Telegram webhook 只使用专用 secret header。

### Kelivo v3 提交前语义补充

Kelivo 的 `prepare` 阶段会一次性冻结服务端 persona、persona hash/source、客户端完整
messages、当前请求新建的 system/developer snapshot 标识、temperature/max_tokens、mapping
revision 与 generation correlation。内部 `/loop/chat` 只接受这份带版本号的
`provider_messages`，不会再次追加运行时 persona、canonical history 或旧 active snapshot。
客户端附带的历史只参与本次生成；canonical history 仍只写最后一条真实 user 和最终 assistant。

相同 `Idempotency-Key` 的并发请求在短期 key 锁内完成 lookup/prepare：相同 payload 若正在
prepared/dispatching 返回 `idempotency_in_progress`，不同 payload（包括原始空白差异）返回
`idempotency_conflict`，completed 则回放原结果；这些状态不会被普通 generation queue 的 429
覆盖。不同 key 才竞争并发队列。

启用 Kelivo 时必须严格满足
`KELIVO_DISPATCH_STALE_SECONDS > LOOP_MODEL_TOTAL_TIMEOUT_SECONDS + KELIVO_QUEUE_TIMEOUT_SECONDS + SQLITE_BUSY_TIMEOUT_SECONDS + KELIVO_COMPLETION_COMMIT_MARGIN_SECONDS`。
进入真实 provider dispatch 前会原子写入 `dispatch_expires_at`，reaper 只处理已真正过期的
dispatching 行。Kelivo 关闭时不检查 Kelivo 专属跨字段关系，`/v1/*` 仍保持不可用。

发布 v3 前必须备份完整 `relay.db`。若曾运行未提交的旧 v3 测试 schema，应删除测试数据库，
或从 v2/上线前整库备份恢复；不能只修改 migration marker，也不要让启动逻辑猜测修复可疑结构。

### Memory Phase 1.5 PR A：v8 request ledger（仅基础设施）

Migration v8 仅新增 `memory_action_requests`、一个显式索引，以及无条件拒绝
UPDATE/DELETE 的两个 immutable triggers，不修改或重建 v1–v7。validator 精确要求
两个 trigger 的集合与 SQL fingerprint；缺失、修改或多余 trigger 都 fail closed。
terminal row 只允许 INSERT once，不存在 UPDATE 路径。Memory Core 启用为 read-only
时会原子应用并严格验证 v7–v8；这不需要 fingerprint Key ID/HMAC Secret，也不会启用写入。

v8 ledger 只保存受限 request ID、闭集 action/origin、32-byte
domain-separated HMAC binding、公开 target/result Memory key、terminal
status/category 与 UTC timestamp。它不保存 Memory/canonical 正文副本、
fingerprint、外部用户/设备/session identity、完整 metadata、capability/signature、
Runtime Authority、Secret/Key ID、SQL 或异常正文；没有持久化 `processing` 状态。
terminal HMAC 同时绑定 canonical reference、结果和版本化
`TerminalSemanticSnapshotV1`。snapshot 在同一个外层 `BEGIN IMMEDIATE` 中从实际
canonical、evidence、memory_sources、target/result item 和相关 suppression 行构建，
验证正文/role/channel/source、evidence ownership/action、provenance link、
kind/scope/sensitivity/state/supersession 与 request/Store capability 完全一致。
snapshot 原文不落库、不记录、不返回；只保存 HMAC digest。首次完成不符返回固定且
data-free 的 `terminal_semantics_invalid` 并整笔回滚；replay 使用相同 validator
重建 snapshot、重算 HMAC 并 constant-time compare，任何 referenced-row 篡改、增删或
result-key 重指向均 fail closed，且不会执行新 request。

terminal category 和 result key 不再由 caller 传入。Store 的私有 savepoint
完成后，`MemoryStore` 先记录一个可供 snapshot/replay 使用的 frozen、slots、
repr-safe `StoreOutcomeSemanticsV1`。它只携带 action/outcome、target/result item、
当前 evidence/source、精确相关 suppression IDs 和 contract version；随后由当前
UoW 将它包进仅用于首次完成的 sealed `TrustedStoreOutcomeV1` live envelope。
envelope 绑定每个 UoW 唯一的 owner token、Store object identity、request ID、
canonical message ID 和真实 capability/action ID。`complete_request()` 保持零参数，
并在 snapshot/HMAC/terminal INSERT 前再次核验所有 owner 字段以及
`outcome.action_id == sole deferred action ID`，然后才从 semantics 的唯一闭集映射
派生 terminal category/result key。cross-UoW、cross-Store、cross-request、
cross-canonical 和 cross-action-ID 移植全部 fail closed。

每种 outcome 都固定自己的 suppression snapshot：remember/correct 的 suppressed
绑定实际命中的 request/replacement fingerprint；corrected 绑定
`corrected_obsolete`；forgotten 与 already_forgotten 都绑定目标 Memory 的精确
`user_forget` suppression。缺失、替换或增加影响这些关系的行会使首次完成或 replay
fail closed。

Replay 先把数据库实际 terminal row 解码为严格的 `StoredTerminalRowV1`，逐字段比较
实际 `request_id/action_kind/origin/target_memory_key` 与 caller binding，再使用 row
中实际的 canonical/status/result/category/created_at/updated_at 重建 terminal
payload、重建当前 semantic snapshot、重算 HMAC 并 constant-time compare。不得用
caller binding 的 request columns 代替数据库值。immutable triggers 保护正常写入；
即使测试中离线移除 trigger、篡改 row、再恢复完全相同的正式 trigger，HMAC/snapshot
仍会检测语义变化。Replay 只重建 `StoreOutcomeSemanticsV1`，不会伪造 live
`TrustedStoreOutcomeV1`；owner token/Store reference 不持久化、不进入 HMAC、
不写日志或响应，也不需要在 restart 后重建。

本阶段只增加可信 composition root 将来可使用的内部 Unit of Work。它让 ledger、
新 canonical action、capability 验证和 Memory Store 写入能够共享一个
`BEGIN IMMEDIATE`，并在 capability 消费前核对实际 Store action 与已 claim request
的 action、canonical reference、target、scope、kind、正文和 sensitivity 完全一致；
但没有实现 Explicit Memory Entry Service，也没有 CLI、MCP、HTTP、Telegram 或
Operit 入口。正式 App 仍只保留 read service；现有聊天路径不会执行 Memory
write。该 Unit of Work 的 owner token 与 exact-type 检查用于防止受审 composition
path 的误接线和跨 owner 移植，不是恶意同进程 Python sandbox。

此前复审发现的 terminal immutability、referenced semantics、真实 Store outcome
mapping 与实际 stored request columns 重验均已完成。随后最后一个定向 Medium 指出
live outcome 未绑定 owning UoW/Store/request/canonical，且 completion 未再次比较
outcome action ID。本轮已拆分 replayable semantics 与 owner-bound live envelope，
并在 defer、complete 两处核验全部 owner/action identity。该 finding 的定向修复已完成，
仍须等待最后一次聚焦独立复审。PR 必须继续保持 Open/Draft，不得转 Ready、merge 或部署。

本 PR 不批准部署或生产启用。`MEMORY_EXPLICIT_WRITES_ENABLED` 继续默认
`false`，不得为本 PR 配置真实 Memory Secret。旧 v7 代码会忽略 additive v8
表；应用回滚不得手工删除 v8 marker/table。若必须物理降级 schema，只能在停写后
恢复一致的 pre-v8 整库备份。

### Memory Phase 1.5 PR B: explicit action entry（默认关闭）

PR B 增加内部 `ExplicitMemoryActionService`、固定 origin 的 facade 和受审查的
事务组合 backend。它不增加 CLI、MCP、HTTP route、Telegram/Operit/Kelivo/
Galatea adapter、provider/model、outbox 或外部消息。四个 facade 只在 composition
root 内创建，分别固定为 `operator_cli -> web/relay`、`mcp -> relay/mcp`、
`telegram -> telegram/telegram` 和 `operit -> operit_share/operit`；调用方不能
覆盖 provenance、canonical ID、Store outcome、category 或 result key。

每个新 request 在同一个 outer `BEGIN IMMEDIATE` 中完成 target resolution、
完整 binding、claim、全新 server-owned canonical action、固定 privileged action、
Store savepoint、owner-bound outcome/semantic validation、terminal INSERT 与 COMMIT。
同 ID/同 binding 只做 terminal replay；同 ID/不同 binding/action 返回
`request_binding_conflict`。不确定 COMMIT 只用新查询 UoW 查 terminal：存在则
replay，不存在则固定返回 `transaction_outcome_uncertain`，绝不盲目重执行。

Forget canonical 固定为
`Forget explicit memory: <public memory_key>`，binding/capability 的
`normalized_content=None`。Target resolution 与 Store action 使用明确的
metadata-only projection；active 旧正文不会被 SELECT、物化到 Python/SQLite result
row 或 dict，也不会被复制。Terminal snapshot 只读取 tombstone metadata 与 SQL
`IS NULL` 缺失标志，不把正文或 fingerprint 值返回 Python。Canonical、result、
error、repr、readiness 和日志均不含旧正文；restart replay 依赖
target/tombstone/suppression 与已认证 terminal snapshot。Category 与 result key
始终来自真实 Store outcome。

Forget prepare 必须在当前 outer UoW 内调用 Store 的专用 metadata getter。Store
立即把 exact metadata object 注册到该 UoW；claim 再把 registration seal 到 exact
Store/UoW identity、11 个 metadata 字段、request ID、origin、target key 与
request-binding digest。Privileged action 不接受 caller metadata，也不重复查询
target，只能取得当前 UoW 已注册的 exact object；Store 写侧 B 查询会再次逐一比较
11 个字段。伪造、替换、篡改、旧类实例、未注册或跨 Store/UoW/request/origin 的
对象均 fail closed。该 registration 只属于新写入路径：同一 `BEGIN IMMEDIATE`
先按 request ID、固定 origin 与公开 target key probe terminal；只有 row 不存在时
才执行 A、创建 registration，并在 `claim_request()` 再次确认 row 不存在后 seal。
Registration 在 commit、rollback 或 uncertain close 时清除；不进入数据库、
terminal、result、repr 或日志。

已完成 Forget terminal replay 会从实际持久化 request columns 与当前 C tombstone
重建完整 binding，验证 canonical/evidence/source/suppression semantic 与 terminal
HMAC，然后直接 commit lookup UoW。Replay 不执行 A/B，不创建、seal 或消费
registration，不签发 capability，也不调用 Store。Uncertain commit 关闭原 UoW 后
只执行同 binding existing-terminal lookup；terminal 不存在时固定返回
`transaction_outcome_uncertain`，绝不进入 new-request claim 或盲目重试。

Forget 对 `memory_items` 的读取只有精确 A/B/C 三类：A 为 11 字段
prepare/registration projection（仅每个新 request 一次）；
B 为 Store projection（active 在 UPDATE 前后各一次，already-forgotten 一次）；
C 为只含 tombstone metadata 与三个 `IS NULL` absence flag 的 terminal projection
（completion 与每次 completed replay 各一次）。实际 `memory_items` 序列为：
new active `A -> B(key) -> B(id) -> C`，new already-forgotten
`A -> B(key) -> C`，same-process/fresh-runtime/真实两进程 restart replay 与
uncertain lookup 均只有 `C`。Completion 使用真实 Store outcome 携带并与
registration/B row 核对的内部 item ID；replay 使用 C 的已验证 item ID，因此 Forget
不再执行独立 `SELECT id`。Tombstone semantic 无 self-join；source semantic 只查询
`memory_sources`，并从已验证 terminal item 建立 `memory_id -> memory_key` 映射。
测试 gate 不再按 statement keyword 或是否出现 `SELECT` 放行。每个
`memory_items` authorizer event 必须属于精确 A/B/C、精确 content-clearing Forget
UPDATE，或 SQLite 外键校验只读取 `memory_items.id` 的精确 `memory_sources`
INSERT。Gate 分别验证 read/update columns、cursor description、完成状态与 raw SQL
statement key。该 key 只规范化换行、token 外空白与安全标点周围的格式，不折叠或
改写 literal、`?`、quoted identifier、comment、clause 或 expression；合法格式的
Memory key literal 与整数 ID literal 不会被转换成 `?`。未知 literal A/B、
quoted identifier、comment adjacency、schema qualification、CTE、alias、join、
subquery、UPSERT、trigger 与 write `RETURNING` 均被拒绝。Gate 持久 violation/
record 只保存固定 category、registered name 或 `unknown`、安全 schema column、
registered statement description 与布尔状态，不保存 SQL、statement key、数据库
路径、trigger 文本、parameter 或 literal。合法 Forget UPDATE 无 `RETURNING`
且不返回 row。

真实 restart 测试由两个独立 `sys.executable -m` subprocess 共享一个临时 SQLite
文件完成，不再把 in-process fresh runtime bootstrap 称为 process restart。
Subprocess stdin 限制为 16 KiB exact phase schema，拒绝尾随、缺失、额外或错误类型
字段；stdout/stderr/JSON/repr/argv/error 同时检查正文、synthetic Secret、
fingerprint、registration/UoW 类型与对象地址泄漏。永久回归还包括 Forget 同
request 2/4/8 SQLite caller single-winner，以及完整 tombstone/suppression
tamper matrix。真实 completed-replay SQLite case 覆盖 tombstone 的 `id`、
memory key、status、kind、scope、sensitivity、explicitness、confidence、
fingerprint version、updated time、content/fingerprint absence、self/dangling/
valid-target supersession 与 row deletion；suppression 覆盖全字段、删除和结构合法
但归属错误的 replacement。每个 replay 均 fail closed，且不执行 A、registration、
capability 或 Store，不修改 terminal，也不增加业务表。

环境变量保持：

```dotenv
MEMORY_CORE_ENABLED=false
MEMORY_EXPLICIT_WRITES_ENABLED=false
MEMORY_EXPLICIT_ENTRY_ENABLED=false
```

只有三者都为 true，且 fingerprint Key ID/HMAC Secret、profile、v7/v8 schema
全部有效时，App 才构造内部 entry backend 与四个 facade。否则没有 authority/
writer、没有 route writer，并由独立的 `memory_explicit_entry` readiness 状态
fail closed；entry-only 错误不破坏 `memory_core` 的只读 readiness。

PR B 不修改 DDL、migration tuple 或 schema version：
`migration_v9_needed=false`。本 PR 不批准部署、生产 Secret、Core/writes/entry
启用或生产 Memory action；生产继续保持 read-only，并等待独立安全复审。

### Memory operator preflight（仅供后续 CLI 组合根）

`backend.memory_operator_composition` 是无 FastAPI 副作用的正式 API；本阶段
不提供 CLI 命令，也不批准部署或启用生产 flags。调用方可先调用
`preflight_operator_memory_from_environment(...)`。它只加载一次部署配置，
使用 SQLite `mode=ro`、`PRAGMA query_only=ON`、`foreign_keys=ON` 和冻结的
busy timeout，完整验证 core/relay v1-v6、Memory v7、ledger v8、精确 marker
集合及 fingerprint profile。数据库不存在时不会创建文件；失败不会运行
migration、recovery、路径准备、brain target 初始化或任何业务写入。

只有 preflight 全部成功后，
`compose_operator_memory_service_from_environment(...)` 才创建进程唯一
Runtime Authority，并只执行
`create_entry_backend(runtime.privileged_actions)` 与
`bind_operator_cli(backend)`。返回值是 repr-safe 的
`ExplicitMemoryActionService`；不会绑定 MCP、Telegram、Operit，不会创建
HTTP/provider/network/outbox 入口，也不会单独返回 Authority/writer 或把它们放入
公共 API、repr 或日志。Exact service 内部的 reviewed backend object graph 仍按
既有 trusted-same-process threat model 持有写权限；该边界不是 Python sandbox。

该 API 不改变上线流程或生产默认值。不要为本变更配置真实 Memory Secret，
不要启用 `MEMORY_EXPLICIT_WRITES_ENABLED` 或
`MEMORY_EXPLICIT_ENTRY_ENABLED`，不要对生产 SQLite 或备份运行 operator
preflight。`migration_v9_needed=false`。
