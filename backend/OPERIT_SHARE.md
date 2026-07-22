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
必须不同于 `KELIVO_API_KEY`、relay、Telegram、audit 和 provider secrets。
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
tool role、content parts、data URL 和任何多模态/附件结构都会被拒绝。

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

服务端按固定 Operit client、canonical session、model alias、规范化正文和固定
`channel=operit_share`/`source=operit` 生成自动幂等指纹。在 replay 窗口内，网络重试
最多产生一次 provider dispatch；完成后返回持久化的同一响应和 usage。并发竞争请求
可能先得到 `409 idempotency_conflict`，随后重试会命中 completed replay。
`prepared`、`dispatching` 和 `dispatch_uncertain` 都不会重新 dispatch。

成功后最多新增一条 canonical user 和一条 assistant message，两者 meta 固定包含：

```json
{"channel": "operit_share", "source": "operit"}
```

日志不得包含分享正文、完整 URL/query、Authorization、key 或 key 前缀、session、
Telegram 身份或 Operit 设备身份。MVP 不保存 Operit conversation/device/Android ID。

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
7. 系统分享只预填内容，不自动发送；用户检查正文后手动点击发送。
8. 客户端等待超时至少设为 180 秒。自动网络重试由服务端幂等保护。

设备安装版本与 Android 系统分享行为仍需在真机手工验收。MVP 只支持文本和 URL；
视频理解、媒体下载、文件处理、短链展开和 URL 抓取全部留到 Phase 2。

## 上线边界

此功能提交本身不修改 Render、不设置生产环境变量、不部署、不启用 Heartbeat，也不
调用真实 provider 或发送 Telegram 消息。启用生产配置前应先做数据库一致性备份、
真机手工验收和受控部署；回滚只需恢复旧 deployment，并保留现有 v1-v6 数据库。
本 MVP 不需要 migration v7。
