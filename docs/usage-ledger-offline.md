# 归汀用量账本：离线首版与后续清单

入口固定为 **会话选项 → 用量账本**。默认当前会话和当前运行模式，
页面提供全部会话、API / Codex、模型、日期 / 小时筛选。既有上下文用量入口保留。

## 本批的执行边界

- 独立分支 `codex/usage-ledger-offline-20261023`，草稿 PR，不合并、不部署生产。
- 新模块 `backend/usage_ledger.py` 只使用独立 SQLite，不导入记忆、SDK、
  生成器或 HTTP 客户端。`usage_ledger_routes.install` 只由离线 fixture 显式挂载。
  候选入口现已加入默认关闭接线，线上未部署、未启用，也不回填历史消息。
- 本批增加 `usage_ledger_capture.ApiCollector`，只由离线测试显式注入
  `ApiWebRuntime(legacy, usage_collector=collector)`。默认参数为 `None`，
  既有生成器、独立记忆任务和正式运行不会自动启用采集或缓存参数。
- Codex 后续批次增加 `usage_ledger_codex.CodexCollector`，显式注入
  `StreamingCodexGenerationRuntime(..., usage_collector=collector)`；正式入口不创建它。
  只被动接收原生通知，不增加 RPC，不调用模型、不升级 CLI 或改写线程内容。
- 不发送真实模型 API 请求，不读取生产聊天或独立记忆验收题、答案、标签、预测。
- v48 冻结状态、36/36 与 108/108 门槛、现有提示词和记忆策略均保持原样。
- 验证使用合成用量、临时数据库、ASGI Mock、CI 中 Chromium/WebKit 和本地回环 fixture。
  截图标明「离线 Mock 演示」，演示数值不会写入生产账本。

## 功能清单及实现位置

| 优先级 | 功能 | 离线首版 | 后续生产接入 |
|---|---|---|---|
| P0 | 上下文监控 | 独立观测：Token、模型上限、比例、剩余、来源、新旧状态 | 接实际观测；不可用时不拿累计 Token 代替 |
| P0 | 自动换窗预警 | 可配置 Token 阈值和提前空间；历史观测不发当前预警 | 接实际占用与会话预警；自动换窗未启用 |
| P0 | 换窗与压缩记录 | 压缩前后 Token；离线续窗回执分为建窗、收件、完成首条回复 | 续窗登记钩子未挂载生产；重要记忆仍需独立验证 |
| P0 | 单轮 Token | API 逐请求；Codex 原生轮次与连续累计差值离线适配，首次无基线为不可用 | 生产线程独占归属与事件连续性验证后再启用 |
| P0 | API 缓存统计 | 读、写、未命中、按 Token 加权命中率及覆盖率 | 按服务商和返回格式明确选择适配器 |
| P0 | 缓存优化 | 显式服务商配置、稳定前缀 HMAC 和请求缓存参数已通过 Mock；不重排提示词/记忆/工具 | 生产连接能力与费率审核后再启用；无预热或额外请求 |
| P0 | 费用与预算 | 每请求不可变价格快照；Decimal 估算单轮、每日、本月、净节省；预算预警 | 接实际价格/账单来源；硬预算拦截未实现，不声称保证费用上限 |
| P1 | 会话统计 | 只读当前选中版本的真实消息总数、可读取数和用户轮数；离线 SQLite 验证 | 生产安装器注入权威会话解析器；全会话去重统计待定义 |
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
  Codex 原生 envelope 原样保存 `last` / `total` / `modelContextWindow`；另存
  `usage_baseline` 与 `accounting_basis`，CSV / JSON 都可核对原始累计值与差值。
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
不会改变模型结果、触发重试或向前端暴露异常原文。即时健康计数是进程内诊断；
独立 journal 已保存可持久化的未确认标记与固定类别缺口，界面显示保留范围。
数据库彻底不可写时仍无法保证留痕，未发现缺口不证明完整采集。

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

持久化采集状态、保留/删除钩子、明细分页与分批汇总已加入离线分支。
后续仍需做启用前的覆盖范围验收、生产配置及显式启用决定，不自动挂载生产。
Codex 订阅额度仍沿用现有账户面板，不改账户统计协议或模拟 API 计费。

## Codex 单轮与累计计数（离线后续批次）

核对固定 CLI **0.160.0** 的 schema 与官方同版本源码：
`thread/tokenUsage/updated` 携带 `last`、线程 `total` 和可能为空的
`modelContextWindow`。`last` 只用于最近一次上下文观测，不能把多次 last 快照当
成整轮用量相加；线程 total 也不能直接累加进会话账本。
参考 [App Server](https://learn.chatgpt.com/docs/app-server) 与
[固定版本 TokenUsageInfo](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/protocol/src/protocol.rs)。

- 被动订阅 turn/started、turn/completed、thread/tokenUsage/updated 和原生
  contextCompaction 的 item 通知。不会请求隐藏推理内容或使用 internal-only 事件。
- 事件可能早于 turn/start ACK：最多暂存 32 轮（可配置 1–128），先核对 durable
  thread_id + turn_id + canonical_message_id + session_id，再写独立账本。
  只读路由标识与消息存在性，不读取聊天正文、记忆、验收数据或提示词。
- 首次只有原始累计值，没有可靠前一轮终态基线时，整轮消耗为 null。
  下轮必须匹配前一已观测终态、数据库中的紧邻原生轮次、同一会话和模型；
  才能计算 `本轮 total - 前轮终态 total`。因此一轮工具多请求不会重加快照。
- **独占归属是显式前提**：`exclusive_threads` 默认 false。只有配置所有者确认
  这些原生线程由此归汀运行时独占时，才可设 true 使用跨轮差值；其他客户端也可能
  写入同一线程时，保留原始累计值与上下文，单轮差值仍不可用。离线 fixture 显式为 true。
- 进程观察 epoch 改变、漏掉中间 durable 轮次、事件次序异常、累计字段回退、压缩、
  未观察到开始或终态，都会阻断跨轮推算。缺失推理计数为 null，真实上报的 0 保持 0。
  重启后的重放不会重复入账，也不能把旧账本重放当作新的连续基线。
- 请求标识由 provider + 原生 thread + turn 确定，进程重启不改变它。
  已终态轮次不被迟到事件重写；迟到变化会清除基线并记入采集健康计数。
  不同订阅连接、原生线程、会话和 API 模式各自隔离。已删除消息不能被重建。
- 上下文单独取最近原生 last.totalTokens 和显式模型上限。下一轮开始即使还没有
  usage，也会使旧观测过期。压缩使旧占用失效；压缩后出现新观测才恢复显示。
  压缩完成不等于重要记忆已保留，memory_preserved 仍为 null。
- 原生轮次并非单个 API 请求：API 延迟、独立请求重试/超时/限流及 API 缓存/账单
  不从这些事件推测。界面按轮显示来源说明，额度继续在“我的”查看。

当前暂存与即时健康计数是进程内数据，journal 由后台核对持久化可观测缺口；
关闭/重启导致的缺口不伪装完整历史。权威来源、删除/保留与分页均已在离线 fixture
接入，生产仍需组合挂载、线程独占归属和通知连续性验收。后台保存失败不会重试生成
或改变最终回复。启用前配置审阅见 [覆盖与配置核对](usage-ledger-enabling-review.md)。

费用只估算 Token 部分，工具费、税费、阶梯/区域/批次价等未采集时不保证完整账单。
缓存写入的分 TTL 数值不会与总写入重复计费；价格不全、用量不全或字段冲突时费用不可用。
历史 API 若明确按普通输入费率计缓存写入，必须在该请求价格快照显式声明
`write_in_input_rate: true`，不能因字段缺失就假设写入量是 0。
净节省可为负数，表示写缓存溢价超过了缓存读取优惠。币种之间不相加、不自动换汇。

## 离线接口

### 消息来源与续窗回执（本批新增，默认关闭）

`MessageCounts(relay_db, session_row=...)` 以只读连接和同一 SQLite 快照查询历史：

- 使用后端持久化 `web_version_selections`，校验选中版本、根会话和完整父链属于
  同一运行模式。删除、无效版本、循环父链、读取期间权威行变化均返回不可用。
- 只统计 `in/user`、`in/voice`、`out/reply`；一条用户消息算一轮。工具、状态、
  思考事件与其他候选版本不累计。历史前缀副本在当前版本里计一次，并单列副本数量。
- SQL 聚合没有历史分页或 5000 条分支创建上限；模型筛选不会隐藏消息。
  只读 ID、角色和必要的 meta 字段，不读取聊天正文，不回填或修改消息。
- 「可见」指当前历史中可读取的全部消息，包括屏幕外消息。现有虚拟滚动不是折叠，
  目前没有独立折叠/归档存储，所以这两项保持 null。不能据此显示 0 条归档。
- 全会话范围和与会话权威 provider 不同的模式计数仍不可用，不把 API/Codex 混算。
  返回 `message_counts_provenance` 描述来源、选中版本、计数口径和不可用原因。

`Continuations` 是**服务端可信登记钩子的离线契约**，没有浏览器提交端点：

1. 权威会话工厂创建空的新会话后，在同一创建锁下登记来源、目标、UUID、待发送的
   编辑后文本。只保存带密钥的 HMAC，不保存文本、密钥或模型回复。
2. `target_created` 只表示新窗存在，不声称本机草稿已保存、发送或续窗成功。
3. 只核对目标新窗第一条持久化用户消息；完全匹配且来源为 Web relay 才记录
   `message_received`。首条不一致记为不可验证，不从后续消息中寻找一次匹配。
4. API 需要终态 generation 行、reply_id 与实际 canonical 回复一致；Codex 还核对
   generation、callback、client message 身份，callback_pending 不算完成。
   两种模式都要 `finish_reason=completed` 和正确 reply_to。失败、部分回复、
   不确定的上游、缺失回执都不能记为成功。
5. 阶段事件按请求和阶段去重；登记重放不得改变内容；删除账本来源或目标时一并删除
   关联回执和事件。密钥变化不猜测旧摘要；重要记忆保留始终等待独立验证。

只有离线 fixture 显式注入 `message_source`、`continuations`、`manual_compactions`
和 `continuation_creator`。现有生产入口没有配置这些对象，仍沿用本机续窗草稿。

本批增加 `ContinuationCreation`，通过同一个 `create_web_session` 工厂创建 API / Codex
目标，并调用可信回执登记：

- GET 续窗预览只在显式注入 creator 后返回 `registration.supported=true` 和当前选中版本。
  前端据此 POST `/app/sessions/{sid}/continuation`，严格提交 UUID、编辑后文本和预览版本。
  服务端按来源权威行决定 provider/目标 ID；浏览器不能指定目标或运行模式。
- 同一 UUID 在越过工厂边界前持久化保留，只保存 HMAC 和标识。结果不确定时只读取同一
  目标进行核对，绝不自动再次建窗；内容、来源、版本、密钥不一致则冲突退出。
- 原有 API/Codex 建窗工厂新增仅供可信服务调用的可选 session_id；普通路由不接受该字段。
  Codex 沿用原先的 pin 和发布逻辑，不增加 turn/start 或模型请求。
- 登记成功后前端同步会话、选择目标并放入本机草稿，原窗草稿保留，必须由用户确认发送。
  老后端未声明能力时沿用既有流程；已声明能力但失败时不降级创建第二个窗口。
- 选中版本在预览后变化、会话删除、清除账本与建窗并发时，均不能登记旧回执或重新建窗。

`ManualCompactionReceipts` 通过 `Compactions(..., usage_collector=...)` 或运行时的
`compaction_collector` 显式接入，默认 None，不改变原有压缩协议：

- 同步已有 `web_codex_compactions` 的排队、准备、派发、运行、未知、核对、完成、失败状态，
  核对当前原生 pin、线程和来源权威。ACK、超时或传输不确定不等于完成，不自动重放。
- 压缩前数值仅取匹配最新已完成原生 job、canonical 用户消息和时间的上下文观测。
  压缩后数值只接受同一任务、同一线程/turn、已观察压缩 item 完成后的原生 last.totalTokens。
  不用线程累计 total 或费用消耗代替上下文，字段缺失保持 null。
- 迟到的压缩后数值追加独立观测，不覆盖已记录的终态；排队或派发前失败不使现有上下文
  失效。已派发的压缩会使旧占用失效，新的正式占用仍需后续观测。
- 手动压缩单独留回执，不追加聊天请求用量；没有归汀生成 job 的手动 turn 会打断被动
  Codex 采集器的跨轮基线，不能把压缩消耗混入下一轮差值。
- 清除账本保留仅含会话标识和时间的手动任务清理水位，阻止旧原生历史/迟到通知重建账本。
  会话删除、留存期限与分页钩子已在离线 fixture 验证，生产组合仍未挂载；重要记忆始终
  未被推断为保留。

### 路由

- `GET /app/sessions/{sid}/usage-ledger?mode=api|codex&scope=session|all&model=...`
- `GET /app/sessions/{sid}/usage-ledger/export?...&format=json|csv`
- `PUT /app/usage-ledger/settings/{mode}`：revision CAS 与阈值、时区、各币种每日/月预算。

接口由测试安装器注入鉴权和权威会话解析器，禁止浏览器直接提交用量或事件。
所有响应 private/no-store。配置按模式分开，不改变历史价格。
累计费用与预算按此模式全部会话计算，模型筛选不能藏掉支出。
未有完整费用记录时剩余预算不可用。该版本只预警，不执行硬预算限额。

网页和导出不再使用内部兼容列表方法 `Ledger.records` 的五万条上限。汇总在同一个
SQLite 快照里以 256 条为批次扫描；费用保持 Decimal，累计值不随明细翻页改变。
汇总仍需保存唯一轮次与分组键，不声称内存恒定。超过 2000 个趋势分组时明确提示
缩小范围，主汇总与明细不被截断为假全量。

### 持久化采集状态、分页与保留

- API 匹配请求开始时持久化 `unconfirmed`；入账和 `recorded` 状态在同一事务提交。
  固定异常原因仅带标识/时间，不存路由、请求头、提示词或异常原文。实际数据成功入账
  后清除对应写入缺口。重启不把未确认请求当成失败、零消耗或已经入账。
- Codex 绑定 durable job/canonical 消息后登记。通知线程保留有界异常队列，由后台
  核对流程冲刷；epoch 变化、淘汰、迟到变化与失败留下线索，未归属异常按连接层展示。
  数据库不可写、崩溃前未持久化的原生通知无法承诺完整留痕；始终只统计已观测/保留记录。
- 全模式预算检查全部会话的缺口，模型/当前会话筛选不能隐藏缺口。存在缺口或已删历史
  时剩余预算不可计算；未应用缓存配置是独立诊断，不自动当作用量缺失。
- `GET /app/sessions/{sid}/usage-ledger/records?...&limit=25&cursor=...` 使用签名游标，
  绑定模式、会话、模型、页大小、序号快照和清理版本。追加/回填不挤动旧页，清理后游标
  失效需刷新；每页最多 200 条，前端 25 条。原始数据不会用于前端补造缺失字段。
- JSON/CSV 在一个快照中流式导出全部筛选记录。并发追加不改变本次文件；清理版本变化
  中止旧流。JSON 带保留范围，CSV 带 `raw_removed` / `retained_records_before`，避免把
  已清理的原始用量误认成服务商未上报。
- 仅可信服务端可调用 `maintain(..., preview=True)` 审阅数量，再显式执行清理。
  没有浏览器破坏性维护端点或自动调度，默认全部保留。raw 清理保留归一化数值、原价格
  和原费用；完整清理设置单调截止时间、改变统计范围。旧请求不能重放恢复已清理数据。
- `delete_conversation(..., usage_ledger=ledger)` 和版本删除安装器的可选注入，在原聊天
  删除后清理其账本、采集标记与回执，并停用该会话的后续账本写入。默认 None；其他会话
  和模式保持独立。最小去重/删除标记保留以阻止迟到回调。跨存储无分布式事务，账本清理
  失败时返回未确认，可用同一删除请求核对/补清，不自动重发模型请求。
- 清理是逻辑删除；备份与底层文件留存需另行管理。以上钩子均未在生产入口启用，
  不改变 v48、提示词、记忆内容或原验收门槛。

## 运行与验证

后端：`python -m unittest backend.tests.test_usage_ledger backend.tests.test_usage_ledger_capture backend.tests.test_usage_ledger_codex backend.tests.test_usage_ledger_sources backend.tests.test_usage_ledger_continuation backend.tests.test_usage_ledger_creation backend.tests.test_usage_ledger_compaction backend.tests.test_usage_ledger_storage backend.tests.test_usage_ledger_preflight backend.tests.test_web_context_actions`

启用前配置审阅：`python -m backend.usage_ledger_preflight --mock`。仅核对合成声明，
不挂载路由、启动采集或证明完整账单。详细边界见 [覆盖与配置核对](usage-ledger-enabling-review.md)。

显式服务组合与独立进程连续性：`python -m unittest backend.tests.test_usage_ledger_composition`。
`LedgerServices` 只在离线 fixture 中调用；默认不创建 API/Codex 采集器，挂载、权威来源和
删除/动作钩子都需显式传入。详见 [组合与连续性离线验收](usage-ledger-composition.md)。

候选启停与 relay/loop 接线：`python -m unittest backend.tests.test_usage_ledger_startup backend.tests.test_usage_ledger_proxy`。
默认 disabled 无操作；history 保留历史与删除钩子并记录主动停采，capture 复用显式配置。
候选入口已按显式配置接线，线上仍未挂载；详见 [候选接线与回滚](usage-ledger-startup.md)。

采集测试使用真实 provider 函数的语法树执行片段与 HTTPX MockTransport；不执行
bridge 模块顶层环境读取，不导入记忆模块。另走真实 Web 生成运行时、临时 relay /
ledger 数据库和鉴权 ASGI 接口，验证生成 → 采集 → 查看 / 导出闭环及模式隔离。
Codex 使用合成原生通知、临时 native/relay/ledger 数据库，并禁止出站连接；
前端 fixture 由实际 CodexCollector 生成两轮记录，核对首次 null、后一轮差值与
原始累计导出。不会启动真实 Codex、登录或发送真实模型请求。

前端 CI 的 `test:integration` 在安装受检后端后启动独立临时 fixture，
只绑定 127.0.0.1，显式禁止 Python 出站连接；浏览器只放行两个回环源。
不需要 OpenAI API Key、Codex 登录或任何真实账号。生产代码不导入该 fixture。

`ledger-actions-preview.py` 进一步运行真实上下文路由、权威会话工厂、回执登记、手动压缩
管理器和账本路由。原生 pin/压缩传输为 Mock，禁止 Python 出站连接；浏览器覆盖新旧能力
分支、确认/取消、API/Codex 草稿保留、无自动发送、压缩前后观测和手机布局。

## 核对过的字段来源（2026-10-23）

- https://developers.openai.com/api/docs/guides/prompt-caching
- https://developers.openai.com/api/reference/resources/responses/methods/create
- https://api-docs.deepseek.com/api/create-chat-completion/
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- 既有固定版本 Codex 0.160.0 的 app-server 用量结构。

这些来源用于理解字段语义，不提供或硬编码任何模型的当前价格或上下文上限。
