# Operit Share MVP

`POST /v1/operit/share` 是默认关闭、只接收用户主动分享文本或 URL 的专用入口。
它不是通用 OpenAI 对话端点，不抓取 URL，也不处理图片、音频、视频或文件。

## 服务端配置

默认值：

```dotenv
OPERIT_SHARE_ENABLED=false
OPERIT_SHARE_API_KEY=
OPERIT_SHARE_CLIENT_ID=primary-operit-share
OPERIT_SHARE_MODEL_ALIAS=ouou-home
```

启用前必须已经为 `KELIVO_API_SESSION` 配置并验证唯一的 private canonical
session。Operit 没有自己的 session 配置，也不能由客户端选择 session。
`OPERIT_SHARE_API_KEY` 至少 32 个字符，必须是全新生成的服务端 secret，并且
必须不同于 `KELIVO_API_KEY`、所有 `LLM_API_KEY[_2..4]`、`MINIMAX_API_KEY`、
`TELEGRAM_BOT_TOKEN`、`TELEGRAM_WEBHOOK_SECRET`、`RELAY_SECRET`、
`CHANNEL_AUDIT_HMAC_SECRET`、`API_LOOP_INTERNAL_TOKEN`、
`API_LOOP_EXPECTED_NONCE` 和 `API_LOOP_INSTANCE_NONCE`。
真实 key 只能放在平台 secret store 或权限受限的环境文件中。

关闭时，端点返回 `404 endpoint_disabled`，不要求 key，不创建数据库记录，也不
触发 provider。启用配置缺少 key 或固定身份非法时，进程 fail closed。

## 请求契约

认证只接受：

```http
Authorization: Bearer <OPERIT_SHARE_API_KEY>
```

不接受 query token、Cookie、Telegram token 或 Kelivo key。Operit key 也不能访问
`/v1/models`、`/v1/chat/completions` 或 relay 的其他命名空间。

请求示例：

```json
{
  "model": "ouou-home",
  "messages": [{"role": "user", "content": "https://example.test/post?id=1"}],
  "stream": false,
  "tools": []
}
```

只允许 `model`、`messages`、`stream`、`tools`，以及兼容所需且已严格校验的
`temperature`、`max_tokens`、`stream_options`。未知字段返回 422。
`model` 必须精确等于 `ouou-home`，`stream` 必须为 false，`tools` 只能缺省或为空。
所有 message content 必须是字符串，最后一条必须是非空 user message。
tool role、content parts、`data:`、`content://`、`file://`、`blob:` URL 和任何
多模态/附件结构都会被拒绝。NUL、不可见格式控制字符和仅由组合字符构成的空正文也
不能绕过校验。

服务端只使用最后一条 user content。它会执行 Unicode NFC、把 CRLF/CR 转换成
LF、移除正文首尾空白并保留内部空白，然后形成：

```text
[Operit Share]
<规范化后的分享正文>
```

此前由 Operit 携带的 system、assistant 和历史 user 消息不会进入 provider，也不会
写入 canonical history。provider 上下文只来自服务端固定 session 的 canonical history。
URL 字符串保持原样；服务端不解析、展开、请求、重定向或下载 URL。

## 幂等、安全与错误

服务端自动幂等指纹覆盖 prompt contract version、固定 Operit client、canonical
session、mapping revision、固定 model alias、规范化正文、固定
`channel=operit_share`/`source=operit`、provider model、实际 temperature、实际
max_tokens，以及服务端 persona hash/source。任何影响 provider contract 的这些值
变化都会形成新指纹，不会错误回放旧语义。`stream_options` 在 `stream=false` 下仅为
客户端兼容字段：校验后明确忽略，不转发 provider，也不改变请求语义。Operit 本地历史
始终不参与指纹；NFC 与 CRLF/CR→LF 后相同的正文共享同一指纹。在 replay 窗口内，网络重试
最多产生一次 provider dispatch；完成后返回持久化的同一响应和 usage。并发竞争请求
可能先得到 `409 idempotency_conflict`，随后重试会命中 completed replay。
`prepared`、`dispatching` 和 `dispatch_uncertain` 都不会重新 dispatch。

成功后最多新增一条 canonical user 和一条 assistant message，两者 meta 固定包含：

```json
{"channel": "operit_share", "source": "operit"}
```

日志不得包含分享正文、完整 URL/query、Authorization、key 或 key 前缀、session、
Telegram 身份或 Operit 设备身份。MVP 不保存 Operit conversation/device/Android ID。
应用会在路由和 Uvicorn access-line 格式化之前清空该端点的 query string；Render 启动
脚本同时关闭 Uvicorn access log。任何外层反向代理也必须只记录 `$uri` 或关闭此端点的
access log，禁止记录 `$request_uri`/`$args`。客户端永远不要把 key 或分享 URL 放进 query。

稳定错误 code 包括 `endpoint_disabled`、`authentication_error`、
`invalid_request_error`、`unsupported_model`、`unsupported_stream`、
`unsupported_tools`、`unsupported_multimodal`、`empty_share`、
`request_too_large`、`rate_limit_error`、`idempotency_conflict`、
`dispatch_uncertain` 和 `service_unavailable`。

## Operit Android 手工配置

1. Provider 类型选择 `OTHER`。
2. Base URL/endpoint 填：
   `https://ouou-home-telegram.onrender.com/v1/operit/share#`
3. 末尾 `#` 只用于阻止客户端自动追加路径；不要在其后放 key。
4. API Key 填专用的 `OPERIT_SHARE_API_KEY`。
5. 模型名填 `ouou-home`。
6. 关闭 streaming、native tools 和 tool calls；角色卡使用空工具白名单。
   模型参数中只可按需启用 `temperature`、`max_tokens`；关闭 `top_p`、`top_k`、
   `presence_penalty`、`frequency_penalty`、`repetition_penalty` 和所有自定义参数。
7. 系统分享只预填内容，不自动发送；用户检查正文后手动点击发送。
8. 客户端等待超时至少设为 180 秒。自动网络重试由服务端幂等保护。

### 官方源码兼容性审阅基线

Phase 1.5 只读审阅了 Operit 官方仓库 commit
[`499e235`](https://github.com/AAswordman/Operit/tree/499e23570f0df7b5459290bf5e8bc5e279297f2c)：

- `OTHER` 由 `OpenAIProvider` 发送 OpenAI Chat Completions JSON；`model`、`messages`、
  `stream` 始终存在，只有已启用的模型参数才会加入请求。
- 新配置默认关闭所有标准模型参数；MVP 手工配置必须继续只允许可选的
  `temperature`/`max_tokens`，避免 `top_p` 等 unsupported field 得到 422。
- 空工具列表不会产生 `tools`/`tool_choice`；非空工具列表会产生，因此必须保持工具关闭。
- `EndpointCompleter` 会移除末尾 `#`，只把它作为“不要自动补路径”的控制符；fragment
  不会发送到服务端。
- 官方源码没有给非流式请求添加 `stream_options`；服务端保留受限兼容校验以容忍版本差异。
- 网络重试上限为 5；每次从同一历史、参数和配置重建请求体，因此服务端自动幂等可保护
  逻辑相同的重试。官方 OkHttp connect timeout 为 60 秒、read/write timeout 为 1000 秒，
  所以“至少 180 秒”的手工等待要求充分但不是客户端源码硬上限。

设备已安装版本未知；上述结论仍须在合并后的受控真机验收中确认，不在本 PR 内连接设备。

设备安装版本与 Android 系统分享行为仍需在真机手工验收。MVP 只支持文本和 URL；
视频理解、媒体下载、文件处理、短链展开和 URL 抓取全部留到 Phase 2。

## 上线边界

此功能提交本身不修改 Render、不设置生产环境变量、不部署、不启用 Heartbeat，也不
调用真实 provider 或发送 Telegram 消息。发布顺序固定为：安全审查完成 → PR 转 Ready
并合并 → 新建 pre-Operit SQLite 一致性备份 → 以 endpoint disabled 部署代码 → 验证生产
404 与现有 Telegram/Kelivo → 创建并设置专用 key → 配置 Operit 真机 → 单独启用 endpoint
→ 受控文本/URL E2E。回滚只需恢复旧 deployment，并保留现有 v1-v6 数据库。
本 MVP 不需要 migration v7。
