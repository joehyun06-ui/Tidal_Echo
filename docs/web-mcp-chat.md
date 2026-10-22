# API Web 会话 MCP 调用

本窗插件默认关闭。用户在 PWA 的「插件」入口选择最多 4 个已启用的 Streamable HTTP 插件；选择保存在服务器，带版本冲突保护。选择会把工具说明交给当前 API 模型；只有用户确认某项调用后，对应参数才会发往插件。保存插件配置和保存本窗选择均不主动连接。

端点（沿用 relay 鉴权，全部 no-store）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / PUT | /app/sessions/{id}/mcp | 本窗选择；PUT 包含 revision、config_revision、plugin_ids |
| GET | /app/sessions/{id}/mcp/actions | 最近 24 项工具操作及状态 |
| POST | /app/sessions/{id}/mcp/actions/{action_id} | revision + decision（approve 或 deny） |

每项确认绑定会话、原始消息、插件配置、密钥版本、工具定义和参数，10 分钟过期。执行前重新读取工具定义，再原子领取一次执行权。其他设备、重复点击、配置变化、密钥更换及已终止的生成均不能复用旧确认。取消或过期的结果交回模型；执行结果不确定时结束本轮，不继续推测成功。

支持 Chat Completions 和 Responses 的 function calling，流式及非流式响应；Responses 保留本轮返回的 reasoning 项并请求 encrypted_content。单轮最多 5 次模型请求、8 次工具操作，工具调用串行。工具模式没有自动模型 fallback，也没有工具调用重试。每次模型请求有原来的超时，等待用户确认不占用模型请求超时。用量累加服务实际返回值；缺失时标记不完整。

公网 HTTPS 443、DNS 全地址审查与连接 IP 固定沿用检测适配器。每次调用仍需重新读取列表；2026-07-28 与已支持的历史 Streamable HTTP 协议复用协商。Mcp-Name 按当前协议编码。不会执行 STDIO、旧 SSE、OAuth、自定义认证头、服务器发起的 sampling/elicitation 或 MRTR 追加请求。返回内容仅保留有界文本与 structuredContent，不抓取资源 URL 或媒体。

首版接受有界、自包含的 JSON Schema 2020-12 object；带引用、正则规则或 x-mcp-header 的工具会跳过，最多向模型暴露 32 个工具。未发现支持的工具时中止本轮并提示核对插件，避免把失败当作正常无工具聊天。Codex 原生工具仍待独立适配，GET 会明确返回 supported:false。

动作参数与文本结果是私密会话数据，保存在现有 relay 数据库；密钥仍只保存在专属加密凭据表，不进入动作 API 或模型输入。删除会话会同时删除其本窗选择和操作记录。服务重启取消尚未发出的操作，将已发出但未记录结果的操作标记 unknown；它们不会自动重放。停止只能保证本地停止等待，不能撤销插件已经执行的动作。

验证使用合成模型、合成插件与临时 SQLite，不调用真实生产插件，不运行生产模型。
