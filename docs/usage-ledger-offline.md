# 归汀用量账本：离线首版与后续清单

入口固定为 **会话选项 → 用量账本**。默认当前会话和当前运行模式，
页面提供全部会话、API / Codex、模型、日期 / 小时筛选。既有上下文用量入口保留。

## 本批的执行边界

- 独立分支 `codex/usage-ledger-offline-20261023`，草稿 PR，不合并、不部署生产。
- 新模块 `backend/usage_ledger.py` 只使用独立 SQLite，不导入记忆、SDK、
  生成器或 HTTP 客户端。`usage_ledger_routes.install` 只由离线 fixture 显式挂载。
  生产入口没有安装这些路由，没有创建或注入采集器，也不回填历史消息。
- 本批增加 `usage_ledger_capture.ApiCollector`，只由离线测试显式注入
  `ApiWebRuntime(legacy, usage_collector=collector)`。默认参数为 `None`，
  既有生成器、独立记忆任务和正式运行不会自动启用采集或缓存参数。
- 不发送真实模型 API 请求，不读取生产聊天或独立记忆验收题、答案、标签、预测。
- v48 冻结状态、36/36 与 108/108 门槛、现有提示词和记忆策略均保持原样。
- 验证使用合成用量、临时数据库、ASGI Mock、CI 中 Chromium/WebKit 和本地回环 fixture。
  截图标明「离线 Mock 演示」，演示数值不会写入生产账本。

## 功能清单及实现位置

| 优先级 | 功能 | 离线首版 | 后续生产接入 |
|---|---|---|---|
| P0 | 上下文监控 | 独立观测：Token、模型上限、比例、剩余、来源、新旧状态 | 接实际观测；不可用时不拿累计 Token 代替 |
| P0 | 自动换窗预警 | 可配置 Token 阈值和提前空间；历史观测不发当前预警 | 接实际占用与会话预警；自动换窗未启用 |
| P0 | 换窗与压缩记录 | 事件记录前后 Token、续窗结果、重要记忆验证状态 | 对接实际生命周期；重要记忆等待独立验证，不能用压缩完成推断 |
| P0 | 单轮 Token | 普通/API MCP 流式与非流式请求采集；按 turn_id 汇总；流式快照只保留最后一次 | Codex 线程累计增量、真实环境启用另行验证 |
| P0 | API 缓存统计 | 读、写、未命中、按 Token 加权命中率及覆盖率 | 按服务商和返回格式明确选择适配器 |
| P0 | 缓存优化 | 显式服务商配置、稳定前缀 HMAC 和请求缓存参数已通过 Mock；不重排提示词/记忆/工具 | 生产连接能力与费率审核后再启用；无预热或额外请求 |
| P0 | 费用与预算 | 每请求不可变价格快照；Decimal 估算单轮、每日、本月、净节省；预算预警 | 接实际价格/账单来源；硬预算拦截未实现，不声称保证费用上限 |
| P1 | 会话统计 | 独立消息计数快照、已记录轮数 | 接会话权威计数；不能拿账本轮数冒充全部历史 |
| P1 | 累计 Token | 当前/全部会话、按运行模式与服务商区分 | 补实际采集覆盖范围，不伪造缺失历史 |
| P1 | 历史归档统计 | 可见、折叠、归档独立字段，缺失为 null | 接真实归档存储定义与计数 |
| P1 | API 状态 | 单请求耗时、HTTP 状态、限流、超时、用户中断及采集健康计数 | 生产启用前接健康覆盖提示和重启中断清算 |
| P1 | 模型切换记录 | 模型、上下文上限、缓存策略、配置版本、价格快照差异 | 接配置变更与请求实际模型 |
| P1 | Ledger 页面 | 会话选项内的独立全屏页面，API/Codex 共用组件 | 路由挂载与采集接通后再上线 |
| P2 | 历史趋势 | 按时区的小时/日期、服务商与模型聚合 | 大规模数据分页/增量聚合后续扩展 |
| P2 | 缓存异常诊断 | 命中率下降 30 个百分点时列出前缀/配置差异线索 | 不把线索当因果；缓存过期与服务商路由可能无法确认 |
| P2 | CSV/JSON 导出 | 原始用量与归一化值并存，空值与 0 区别，CSV 公式防护 | 生产数据导出沿用相同鉴权与范围检查 |

## 数据契约

`Ledger.append` 只接受服务端可信采集器提交的**请求终态**：

- 身份：`mode`, `provider_id`（不包含地址或密钥的连接标识）、`request_id`,
  `turn_id`, `session_id`, `at`, `model`, `dialect`。
  采集器另存 `requested_model` 与 `model_source`。服务商返回不同模型时，不沿用
  请求模型的价格；未返回实际模型时只标记请求模型，不宣称确认实际路由。
- 用量：`raw_usage` 原始 JSON 对象、`usage_complete`、归一化 metrics、字段路径和冲突标记。
  保存的是原始 JSON 值，不是完整响应体、聊天文本或逐字节 HTTP 报文。
- 状态：已完成、错误、超时、限流、中断、未知；耗时、HTTP 状态、实际重试次数。
- 费用：请求当时的币种和每百万 Token 费率、价格版本，不能用后来的价格回算旧请求。
- 可选：明确来源的上下文观测、预警策略、稳定前缀 HMAC、缓存策略/配置版本。

去重键为 `(mode, provider_id, request_id)`。相同提交不重复计费，不同内容冲突报错，
不静默覆盖。相同时间戳按入库顺序保持最近一轮。跨请求部分缺字段时，展示已知值和
覆盖比例，不声称全量。空账本的用量是 null，已记录请求数量可以真实为 0。

原始用量限 128 KiB、12 层，拒绝凭据字段。事件、上下文和策略均限制字段；
请求头、错误正文、完整模型响应和提示词不属于账本。前缀只保存调用方提供密钥的
HMAC 指纹，辅助函数返回的正文不能写入账本或日志。

支持显式 dialect：OpenAI Responses / Chat Completions、DeepSeek Chat、
Anthropic Messages、Codex、unknown。Anthropic 总输入需要普通输入 + 读缓存 + 写缓存；
OpenAI 读写和推理为输入/输出内的子集；DeepSeek 命中字段不能与 cached_tokens 重加。
Codex 的 API 缓存和 API 费用为不适用，原始订阅计数仍保留，额度沿用现有账户面板。

## 请求采集与缓存配置（默认关闭）

`RouteProfile` 明确绑定 **endpoint + model + chat/responses 协议**、连接标识、
返回 dialect、不可变价格版本与配置版本。不能根据模型名称、域名或一个兼容接口
猜测计费方式。配置没有密钥字段，采集器不保存实际请求路由或请求头。
未匹配连接不擅自归类，`health().unmatched` 会增加；写入失败计入 `dropped`，
不会改变模型结果、触发重试或向前端暴露异常原文。健康计数目前是进程内诊断，
生产接入前需做持久化缺口记录并在账本显示覆盖范围。

- 普通 Chat Completions / Responses 与 MCP 工具多轮在实际 HTTP 请求边界采集。
  一个工具轮次多个请求分别入账，备用模型尝试也分别入账，不把聚合结果再次计入。
  当前 HTTPX 自动重试关闭，因此每个已观测请求 `retry_count=0`；备用模型是另一请求。
- 流式快照覆盖而不累加。保留最后一个安全 usage 对象；缺失、损坏、未结束、中断
  都不伪造完整费用。响应体、工具结果、提示词、异常文本不进入账本。
- 区分正常完成、错误、429 限流、408/传输/总截止时间超时、用户取消。
  单请求耗时不包含工具批准等待；整个会话耗时与请求耗时不混用。
- `provider_default` 不添加缓存参数；`automatic` 适用于服务商透明缓存，也不添加参数。
- `openai_legacy` 只在显式声明能力后添加稳定 `prompt_cache_key`，可选
  `prompt_cache_retention` 为 `in-memory` / `24h`。
- `openai_responses_implicit` 只在显式声明 Responses 能力后添加稳定键和
  `prompt_cache_options={mode:implicit,ttl:30m|12h}`。不根据模型名自动选择。
  参考 [OpenAI 缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)。
- HMAC 仅观察最前面的 system/developer 段和完整工具定义；排除用户文字和会话 ID，
  不改原文、不排序工具列表、不插时间戳。相同前缀产生稳定键，变化留下诊断指纹。
  不保证命中；实际命中量仍取服务商 usage。配置冲突保留原请求并标记未应用。
- 暂未支持 Anthropic 原生 Messages 请求注入、显式断点、缓存预热；已有 Anthropic
  用量归一化与费用快照可独立测试。Codex 不使用这些 API 缓存配置。

后续接入顺序：Codex 累计计数与终态关联 → 真实压缩/续窗与权威消息计数 →
持久化采集缺口、保留/删除策略和分页 → 最后再决定生产启用与挂载。
Codex `last`、线程 `total` 和上下文占用不能直接相加，本批不改订阅统计协议。

费用只估算 Token 部分，工具费、税费、阶梯/区域/批次价等未采集时不保证完整账单。
缓存写入的分 TTL 数值不会与总写入重复计费；价格不全、用量不全或字段冲突时费用不可用。
历史 API 若明确按普通输入费率计缓存写入，必须在该请求价格快照显式声明
`write_in_input_rate: true`，不能因字段缺失就假设写入量是 0。
净节省可为负数，表示写缓存溢价超过了缓存读取优惠。币种之间不相加、不自动换汇。

## 离线接口

- `GET /app/sessions/{sid}/usage-ledger?mode=api|codex&scope=session|all&model=...`
- `GET /app/sessions/{sid}/usage-ledger/export?...&format=json|csv`
- `PUT /app/usage-ledger/settings/{mode}`：revision CAS 与阈值、时区、各币种每日/月预算。

接口由测试安装器注入鉴权和权威会话解析器，禁止浏览器直接提交用量或事件。
所有响应 private/no-store。配置按模式分开，不改变历史价格。
累计费用与预算按此模式全部会话计算，模型筛选不能藏掉支出。
未有完整费用记录时剩余预算不可用。该版本只预警，不执行硬预算限额。

单次查询超过 50,000 个请求显式拒绝，不截断后伪装全量；生产接入前增加分页与汇总。
上下文/续窗事件、账本删除与保留策略、原始数据留存期限，需在生产采集阶段一并接通。

## 运行与验证

后端：`python -m unittest backend.tests.test_usage_ledger backend.tests.test_usage_ledger_capture`

采集测试使用真实 provider 函数的语法树执行片段与 HTTPX MockTransport；不执行
bridge 模块顶层环境读取，不导入记忆模块。另走真实 Web 生成运行时、临时 relay /
ledger 数据库和鉴权 ASGI 接口，验证生成 → 采集 → 查看 / 导出闭环及模式隔离。

前端 CI 的 `test:integration` 在安装受检后端后启动独立临时 fixture，
只绑定 127.0.0.1，显式禁止 Python 出站连接；浏览器只放行两个回环源。
不需要 OpenAI API Key、Codex 登录或任何真实账号。生产代码不导入该 fixture。

## 核对过的字段来源（2026-10-23）

- https://developers.openai.com/api/docs/guides/prompt-caching
- https://developers.openai.com/api/reference/resources/responses/methods/create
- https://api-docs.deepseek.com/api/create-chat-completion/
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- 既有固定版本 Codex 0.160.0 的 app-server 用量结构。

这些来源用于理解字段语义，不提供或硬编码任何模型的当前价格或上下文上限。
