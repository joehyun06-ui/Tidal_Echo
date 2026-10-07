# Codex 流式回复与停止生成

本改动以 `feat/in-conversation-versions-20261004` / `dd3a8aa` 为基线，只扩展 P3 的 Codex 生成链路。现有模型绑定、只读权限、MCP 隔离、账号控制门禁、API 回复和记忆功能开关保持原合同。Codex CLI 继续使用锁定的 0.160.0；通知与 interrupt 请求用该版本生成的 JSON Schema 校验。

## 运行入口与发布顺序

- relay：`backend.p3_codex_relay_app:app`
- loop：`examples.api_loop_codex_canary:app`
- 先在实际部署分支合入并发布后端两个进程，再发布配套 PWA。不要把旧 `main` 或本功能分支直接覆盖到包含后续记忆修订的生产分支；应先比较生产提交并合并本次差异。
- 原有 `CODEX_CONTROL_ENABLED`、`CODEX_CANARY_ENTRYPOINTS_ENABLED`、`CODEX_GENERATION_ENABLED` 不变。新 relay 入口在已启用的 Codex 能力中添加 `generation_controls: true`；旧入口不声明该能力。
- 回退到先前部署提交即可关闭新通道；生成数据库没有新增表、列或状态枚举。

## 浏览器合同

`GET /app/sessions/{session_id}/generation` 返回 `contract_version: 1`、`api_session` 和 `generation`。没有任务时 generation 为 null；有任务时包含本次 ID、用户消息 ID、可停止标志、终态标志与已保存的回复 ID。内部 thread / turn ID 不公开。

`POST /app/sessions/{session_id}/generation/stop` 仅接受 `{"generation_id":"codex-gen-123"}`。会话和 generation 必须同时匹配。排队任务用条件更新取消；已启动任务只中断其持久化 thread/turn。线程准备中或调度结果不明时暂不允许中断，不猜测 turn、不重发生成。重复 stop 不自动重复调用上游。

`stopping` 表示上游已受理停止请求，`stop_uncertain` 表示结果待核对。只有 worker 收到或恢复确认的终态，才返回 `interrupted`。若回复恰好正常完成，则显示 `completed`。服务重启后从持久化 job 状态恢复，不把进程内停止提示当作确认结果。

SSE 继续使用原 `/app/stream` 和现有鉴权。新增 `reply_snapshot` 事件包含 provider、api_session、generation_id、canonical_message_id、stream_id、epoch、revision、text、ts。文字是截至该版本的完整正文；客户端替换而非拼接，忽略同 epoch 的旧 revision。状态 GET 同时提供当前快照，补回断线漏掉的帧。正式回复沿用 generation_id 作为 stream_id，并替换临时气泡。

只投射有 item 生命周期的 agentMessage 正文，处理 item/agentMessage/delta；item/completed 为最终正文校正。评论、原始推理、工具参数和账号信息不进入这个通道。缓存最多 32 个 turn、每个 64,000 字符、10 分钟，既不写日志也不建立消息以外的文本库。

停止后的非空正文通过原幂等 callback 保存一次，元数据 `finish_reason: interrupted`。未输出正文的停止只记录 job 终态，不伪造 AI 消息。最终落库失败时保留 callback_pending，恢复时只核对原 turn，不重发生成。

## 验证范围

新增投射、精确中断、ACK / 终态区分、会话隔离、重复停止、完成竞态、部分回复落库、HTTP 鉴权 / 代理错误脱敏与 CLI schema 检查。配套 PWA 包含断线补回、刷新、草稿、旧能力回退，以及 Chromium / WebKit 交互检查。自动化使用隔离数据库与伪模型，不发送生产聊天，也不消耗真实订阅额度。

## 图片输入

P3 Codex 能力现在声明 `image_input: true`、`text_only: false`。PWA 沿用鉴权 `/app/upload` 和 `/app/send`，支持只发图片及图片加文字。旧能力响应继续禁用图片入口。

生产 worker 从 canonical Web 消息读取附件，限定 relay 的 `att-*` 本地上传，校验来源、会话、MIME、文件类型和大小（每张最多 8 MiB、每条最多 4 张；PWA 当前一次选 1 张）。外部 URL、查询密钥、路径穿越和符号链接均拒绝。持久化 job 仅保存文字及附件内容的联合摘要；worker 再读、再核对，文件丢失或改变时失败，不静默退回纯文字或 API。

图片按 Codex App Server 官方 `localImage` 输入传递，避免大图片 Base64 超过现有 1 MiB JSONL 边界；不拓宽工具、MCP、网络或文件权限配置。无数据库迁移，原纯文字摘要与任务恢复兼容。

协议依据：https://learn.chatgpt.com/docs/app-server （Turns 的 localImage）；新增输入同时通过已安装 CLI 0.160.0 生成的 schema 校验。自动化只用合成图片与隔离伪模型。
