# 用量账本 · 启用前配置与覆盖审阅

本轮只审阅代码和合成配置。**生产采集尚未启用，未读取生产环境变量、数据库、聊天或
真实账户用量，也未进行真实模型请求。** 文中的“已验证”仅指离线测试，不等于实际
部署已经具备这些能力。v48、36/36、108/108 继续冻结。

## 候选分支组合入口（线上版本仍冻结）

- `examples/api_loop_codex_canary.py` 已按默认关闭的 `load_loop` 结果构造 runtime，
  返回 None 时展开空 kwargs，维持原关闭态行为；只有明确配置才注入采集/回执服务。
- `backend/p3_codex_relay_app.py` 按同一 mode 选择是否安装内部转发；缺省不新增路由。
  loop 继续拥有唯一会话权威。只合并前端不能让正式账本产生真实数据。
- `usage_ledger_routes.install` 接收独立账本、鉴权、权威会话解析器和可选来源钩子，
  候选入口按 mode 显式配置，离线 fixture 用合成计划验证该组合。
- 删除与版本删除按服务对象是否存在注入同一账本；线上未部署这些修改，不能写成生产已接入。
- 默认 kwargs 的 AST 检查、关闭态导入检查及三种模式的隔离导入共同验证候选入口；
  不宣称扫描了所有动态加载器、部署实例或未来路径。部署实际配置仍需单独核对。

## 覆盖矩阵

| 来源 | 已有离线证据 | 正式接入条件 / 不覆盖内容 |
|---|---|---|
| Web API 普通聊天 | `ApiWebRuntime._run` 在创建任务时传递显式采集作用域；真实 Chat / Responses 请求函数 + MockTransport | 必须向同一 Web runtime 注入采集器，且每条主/备用路由按 endpoint + model + protocol 精确匹配 |
| Web API 的 MCP 多轮 | `api_mcp_model` 每个模型 HTTP 请求分别采集，不重复计算聚合用量 | 仅记录模型请求；MCP 服务自身的收费与其他工具费不在账本内 |
| Web Codex | 绑定 durable job、canonical message、thread / turn 后才入账 | 必须注入采集器及后台核对；首次无基线、重启、漏轮、压缩或共享线程时，单轮用量可能不可用 |
| 手动压缩 | 同一任务/线程的前后上下文与确切完成回执 | 另行注入压缩采集器；完成不证明记忆保留，不把压缩混入下轮 Token |
| 续窗 | 权威工厂创建、登记 HMAC、核对首条消息与完成回复 | 创建器与回执服务一起接入；不自动发送；重要记忆是否保留仍等待独立验证 |
| 历史消息计数 | 权威选中版本的 SQL 计数，不读取正文 | 需同一权威来源；折叠/归档无独立来源时不可用，全会话去重尚未定义 |
| Telegram / channel bridge / Kelivo / Operit / 后台任务 | 不在当前 Web 采集作用域内 | 即使复用了底层 HTTP 函数也不会自动归属到账本；本轮不扩展这些入口 |
| 外部直连 API / 其他 Codex 客户端 / 启用前历史 | 不可观测 | 不代表账户全部用量，不从累计余额或旧聊天推算回填 |
| 官方订阅账单、非 Token 费用 | 不适用 / 未采集 | Codex 额度沿用“我的”；API 费用仅为已保留请求的价格快照估算 |

“全部会话”只放宽已记录账本的会话筛选，不能解释成整个账户、全部来源或完整历史。
没有观察到异常也不能证明采集完整。数据库彻底不可用、崩溃前未持久化的通知无法
保证留痕；价格缺失、字段冲突、清理范围与采集缺口都必须继续显示。

## 离线配置核对工具

```bash
python -m backend.usage_ledger_preflight --mock
python -m backend.usage_ledger_preflight --config /path/to/credential-free-plan.json
```

`--mock` 只生成合成声明；不加载 `.env`、服务商配置、SDK、数据库或生产入口。
`--config` 仅读取显式指定的一个 JSON 文件，最多 64 KiB，重复键与额外字段拒绝。
它不是部署命令，也没有 enable 参数。退出码 0 表示声明一致，1 表示声明缺失/冲突，
2 表示文件/结构无效；错误只返回固定类别，不回显路径、URL、模型、连接 ID 或原始错误。

输入结构以 `backend.usage_ledger_preflight.mock_plan()` 为合成示例：

| 字段 | 核对内容 |
|---|---|
| `schema_version` | 固定整数 1，不接受字符串或布尔值 |
| `storage` | Linux/Render 绝对路径声明：persistent_root、ledger_db、relay_db，可选 codex_db。账本必须在持久根目录内且与来源库分离；拒绝相对路径、`..` 等 |
| `hooks` | authenticated_routes、session_authority、message_counts、session_delete、version_delete、retention_reviewed、continuations 七个布尔声明；false 单列缺项 |
| `api.routes` | 显式声明 web_chat / web_mcp 来源及 endpoint、model、protocol，不接受 key。每一条备用模型和协议变化也必须列出 |
| `api.profiles` | 直接复用 `RouteProfile` / `CachePolicy` 的校验，核对精确匹配、重复项；允许缺少价格或 unknown dialect，但披露数量且不声称费用完整 |
| `api.fingerprint_key_configured` | 只接受布尔声明，禁止在计划中提交 HMAC 密钥；运行时真正的密钥验证仍由采集器完成 |
| `api.dynamic_routes_reviewed` | 是否已审阅运行期间路由变更的重新核对流程，不能只核对启动时的主模型 |
| `codex` | provider_id、固定适配版本 0.160.0、exclusive_threads、max_pending、durable_binding、background_reconciliation、manual_compaction；未声明独占时允许上下文观测但禁用跨轮推算 |
| `api` / `codex` 为 null | 该模式未计划接入，不要求另一模式的专属配置，并列出未计划的 Web 来源；不能两者都为空 |

路径检查仅验证字符串关系。它**不证明**目录实际挂载、可写、符号链接/硬链接没有别名、
磁盘容量或备份策略正确。路由清单和 hooks 也只是调用者的声明，不是线上验证结果。
工具总是返回 `production_verified=false`、`enables_production=false`、
`billing_complete=null`，即使所有声明一致也不改变这些字段。

实际启用前还缺：部署组合与鉴权审阅、物理存储/备份核对、配置变化清单、观察连续性验收，
以及显式启用决定。这些缺项不会通过配置里填 true 自动消除。本轮不代替这些验收。

已补充 [账本原生备份与隔离恢复演练](usage-ledger-backup.md)：采用 SQLite backup API，
只创建新目标并恢复到临时隔离目录。验证 WAL、价格、签名游标、删除/清理标记、续窗
回执外部密钥及重建原生基线。命令行仅支持合成 Mock，不提供生产覆盖或自动启用。
旧备份不含后来发生的删除，完整性通过也不能直接恢复上线。此项不等于生产备份验收。

## 存储和线程归属的离线补强

计划核对现已把每个 SQLite 主文件及其 `-wal`、`-shm`、`-journal` 文件一起比较。
账本不能占用来源库的辅助文件名，反向冲突和来源库之间的冲突也会被列为配置不一致。
这项检查仍是纯字符串核对，不打开数据库或读取环境。

候选启动组合在创建账本前检查这些文件的路径与现有元数据：相互碰撞的文件名、已知
符号链接/硬链接别名（设备和 inode 相同），以及非常规文件占用会被拒绝；未存在的独立账本文件允许
后续初始化。检查不读取来源库正文，不执行写入探针，也不能抵御检查后的文件替换或
证明持久挂载、写权限、容量、备份和恢复已经验收。

Codex 核对在同一来源库读取快照中要求线程只有一个会话归属、该线程/轮次只有一个
持久任务，且任务会话与线程归属一致。ACK 尚未持久化仍等待核对，不择取最大任务 ID
来消除歧义。发现冲突时不登记上下文或用量，留下未分配的 `binding_conflict` 证据，
使会话筛选也能看到该模式存在采集缺口；仅破坏冲突线程的差值基线，不影响其他线程。
即使之后归属恢复唯一，也不恢复冲突前基线，须以后续连续观测重新建立。

来源 SQLite 以 `mode=ro` 查询，只读取路由身份、模型和消息身份等已有元数据，不读取
正文或编辑来源表；错误来源路径不会新建空库。这不是两个来源库之间的分布式快照，
也不证明外部客户端没有同时使用线程。`exclusive_threads` 仍是运营方必须核验的前提，
真实账户/CLI 归属、通知连续性、物理存储和备份验收仍未执行。没有调用模型或自动启用。

新增回归使用临时文件、合成 SQLite 和合成通知，覆盖辅助文件碰撞/别名、冲突前后基线、
独立线程、缺失来源库和 URI 保留字符：

```bash
python -m unittest backend.tests.test_usage_ledger_paths backend.tests.test_usage_ledger_codex.CodexLedgerTests
```

已有组件现已整理为显式的 `LedgerServices` 组合，并通过独立实例与真实子进程的合成
重启验收。候选入口已默认关闭接线，线上未部署，真实存储/归属和通知连续性仍未核验；详见
[组合与连续性离线验收](usage-ledger-composition.md)。

## 验证

```bash
python -m unittest backend.tests.test_usage_ledger_preflight
```

测试覆盖备用模型/协议/endpoint 漏配、同路由不同 Web 来源、重复配置、未知收费字段、
缺少钩子、非独占线程、版本不符、模式隔离、路径冲突、密钥字段拒绝、固定错误、输入不变、
账本关闭态不额外读写文件/数据库或访问网络，以及显式模式下受审入口的挂载/注入。
所有输入均为合成数据。
