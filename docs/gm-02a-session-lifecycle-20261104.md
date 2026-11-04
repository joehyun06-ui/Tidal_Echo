# 归汀 GM-02A：会话生命周期契约、离线状态机与重启恢复

日期：2026-11-04。依据 G3/Ombre/kimi 执行方案 v1.1，接续 GM-01A2。

**结论：新增 38 项生命周期合成测试、既有 77 项 G3 回归通过；真实 API/Codex 运行时尚未接入，生产 HOLD。**
本轮没有合并、部署、生产迁移、调用模型、创建用户聊天或发送任何消息。

## 1. 现状核对与实现范围

新开发候选以 Draft PR [#201](https://github.com/joehyun06-ui/Tidal_Echo/pull/201) 的
`4f3f2bd1a0d4fc9533c37ea48a48e73453938363` 为固定基线；新 PR 指向 #201 的开发分支，
不修改 #199/#200/#201 原分支。生产目标分支本轮读取仍为
`c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4`，没有据此重新证明线上部署 SHA。

| 既有组件 | 实际观察 | 本轮遵守的边界 |
|---|---|---|
| `WebSessionProviderAuthority` | 可见 Web 会话/provider 权威在 api-loop 配置；删除有 tombstone；provider 不从 UI 猜测 | 测试复用真实类读取合成 legacy 配置；没有另造生产会话权威 |
| `web_message_versions.py` | alternatives 保留隔离的底层 context；`web_version_selections` 决定 root 当前版本 | 生命周期绑定 root conversation + 固定 selected version；选择变化拒绝旧 closeout |
| `web_session_context.root_id` | alternatives 可映射回 visible root；会话设置在原 SQLite 中 | 不把底层 window/model run 当作新的用户聊天 |
| `ContinuationCreation.create` | 会另建 target session，本身不发送草稿 | 不调用它实现自动换窗 |
| `Compactions` | 有 durable 人工压缩记录及 unknown/reconcile 边界 | 不把人工压缩当作 closeout/reentry 已完成 |

相关源码 blob：会话权威 `c4cef0dfa5e58e511262fdb3195a7c5726eace61`；消息版本
`edb8c7995a9e03140b728e54ebbf549cf0749719`；会话设置 `f4057dbdfd2fa0ba99b4c0735a1fad3c9bac3cc3`；
续窗创建 `18a3c285eb7b28617bdc34b620459daa928a2065`；人工压缩 `7cc51eeb02091671da496c4ebd2b52f63241ee79`。

新增三个文件：

- `backend/session_lifecycle.py`：协议、不可变记录、默认关闭的状态机；没有数据库路径、迁移或运行时入口。
- `backend/tests/test_session_lifecycle.py`：独立合成 canonical SQLite、真实会话权威类、G3/队列/runtime 观察夹具。
- 本文档：契约、故障证据、真实接入缺口与下一断点。

**物理存储限制：**生产会话配置与消息库分处配置文件和 SQLite。本轮测试用 SQLite-backed
legacy surrogate 在一个临时 canonical 文件中模拟它们，新增操作状态/幂等回执也放在同一文件。
这只证明测试适配器下的状态机、事务与进程恢复，不证明真实跨文件提交原子性、跨进程锁或 G3 跨库快照。
没有对真实会话配置或生产 `relay.db` 安装任何表。

## 2. 标识与游标契约

| 字段 | 含义 | 禁止混用 |
|---|---|---|
| `conversation_id` | 现有可见聊天的 canonical root ID | 不能在续窗时另建一个用户聊天代替 |
| `version_id` | 选中的 canonical message alternative | 不能在旧版本 closeout 上静默接入新版本 |
| `window_id` | 一个逻辑聊天内部的底层上下文窗口 | 不是新的 Web session；不可重用已使用窗口 |
| `model_run_id` | runtime 真实观察到的执行实例 | 新窗口确认时不能复用旧 run ID |
| `context_epoch` | 逻辑窗口代数 | 只在已确认激活后 +1；prepared 不前进 |
| `SourceCursor(generation, position)` | G3 来源历史的事件消费位置 | 不能把旧 generation 的游标带到新历史 |
| `message_cursor` | 当前 selected version 的 canonical message ID 水位 | 不能冒充 G3 cursor 或另一版本的消息进度 |
| `closeout_id` / request ID | closeout 与各命令的持久幂等键 | 同 ID、不同参数拒绝；相同重试只返回历史回执，不重做外部动作 |

`SessionSnapshot` 区分 runtime **已持久消费的 observed cursor** 与来源/消息的 **当前 head**。
例如旧 runtime 已消费 source=2/message=1，停顿后新 head=3/2：closeout 记录 2/1，reentry
必须扫描 2→3 与 1→2 才能前进。不能直接把当前最大 ID 写成“已处理”，否则会跳过未消费事件。

`stamp` 是可信组合适配器的一致性声明，必须覆盖会话、版本、队列、runtime 边界、G3 修订和 ACL。
它不是签名、第三方时间或独立可信 C；真实适配器尚未提供此保证。

## 3. 状态机与提交规则

| 当前阶段 | 操作及成功条件 | 持久结果 |
|---|---|---|
| 初始 / `active` | `closeout`：可信 safe point，当前 window/run/epoch 匹配；无 stream、进行中工具或未知 dispatch | `closed`；保存 consumed cursors、pending、active state、摘要候选、候选证据 ID、边界 stamp 与三个检查结果 |
| `closed` | `reentry`：精确 closeout ID、新 target window、完整 delta 扫描与当前来源验证、预算通过 | `prepared`；保存精确 source refs、canonical message ID 引用、pending 与 packet SHA-256；不激活、不发送 |
| `prepared` | `reentry_delta`：显式携带准确的双游标；全部页有效；来源/队列终检通过 | 原子更新 capsule 与双游标；同 request ID 重试不重复计入 |
| `prepared` | `activate`：可信 runtime 观察到 confirmed receipt，且当前 runtime 本身也匹配 target/run/epoch/consumed cursors | `active`；conversation 不变，window/run 替换、epoch +1；pending 仍保留 |
| 任意拒绝路径 | unknown/failed/错绑定回执、忙边界、源/ACL变化、版本变化、游标冲突、预算超限、提交冲突 | 不回报接续成功；提交前失败回滚状态与回执；提交后回执丢失用同 ID 核对 |

`LifecycleReceipt.phase_at_apply` 是**当时命令**的阶段，不代表当前阶段；历史 closeout 回执重放
不能把已 active 的状态倒退。`replayed=True` 明示幂等重放。`reply_completed` 始终为 false：
窗口接续确认不是模型已经回复用户的证据。

每次操作先进入既有会话的 commit fence，读取可信组合快照，再原子提交状态与幂等回执。
仓库通过 expected revision 做 compare-and-swap；快照终检不为严格 bool true 或状态改变均拒绝。
运行时协议只提供 `read_receipt`，没有 dispatch、send、create 或自动 retry 方法。

### closeout / reentry 内容的权威等级

- `pending` 仅保存 canonical 队列的 ID；缺失旧 pending 时，必须有 durable settled ID 说明其完成、取消或失败，不能无故丢弃。
- `active_state`、`summary_candidates` 是有界派生候选，不是 G3 原始事实，不生成新摘要或自动记忆。
- closeout 保存锚点/近期证据的**候选 ID**；reentry 才逐条回源、核对当前 revision/hash/ACL，并要求锚点为已审核 fixed 投影。
- 验证所得证据只持久化 `source_refs`；正文预览每次读取时重新验证，不自动复制 G3 原始正文到 lifecycle record。
- 新消息只保存 canonical message ID 引用，供未来真实适配器按当前版本/权限重新打开；不复制或改写聊天历史。
- `ReentryPacket.authority="derived"`，候选摘要保持 candidate 字段，不得显示为“已核验事实”。
- anchor/recent 选择由可信组合适配器提供；真实选择策略尚未集成。推进来源游标只表示已遍历区间，不表示把全部来源正文注入上下文或证明召回完整。

## 4. 增量、权限和资源边界

`LifecycleAuthority.read_delta` 的页同时声明 source/message 两个区间。检查 stamp、起点、终点、
严格顺序、重复事件修订/消息 ID、终止状态和进展；一条轴可保持不动，另一条前进。任何一页失败
都不推进记录中的任一游标。packet 读取和 activate 遇新 head/pending/证据选择变化，要求先补 delta。

来源修订、权限撤回或摘要/锚点预算变化时，不使用未验证缓存。真实来源完整性仍依赖受审适配器；
自洽的遗漏声明无法由消费端单独识破，本轮不声称源事件完整性或不可回滚认证。

候选限制：最多 100 页；每页 source/message 各最多 100 项；一次 source 变更最多 1000；
累计新增消息引用最多 128；anchors 最多 8、recent 最多 16；窗口历史最多 128。
packet 默认 32768 bytes、最大 65536 bytes。超限整体拒绝，不静默截断 pending、消息引用或证据。
这些是离线保护参数，**bytes 不是模型 token 估算**。

没有可靠 context usage 集成或自动触发器。v1.1 的 500K/600K/700K/800K 仍是指定容量假设下的
设计阈值，本轮不以它们启动换窗。新类默认 `enabled=False`、支持 provider 集合为空，调用即拒绝。

## 5. 离线测试与证据

环境：Python 3.12.14；新增代码无第三方依赖。所有夹具数据均为 synthetic。测试阻断 socket/DNS，
子进程只导入合成 worker，不加载生产入口。测试会真实提交/关闭/重开临时 SQLite；另用两个新
Python 进程验证 closeout→reentry 恢复及同 request ID 重放，保留同 conversation 和 pending。

```bash
python -m unittest \
  backend.tests.test_session_lifecycle \
  backend.tests.test_g3_projection \
  backend.tests.test_g3_projection_feed
```

本地 **38/38 新生命周期测试 + 77/77 既有 G3 回归 = 115/115 通过**。这是独立开发集合，
不重计 G3-QU 正式 36/36、108/108，也不增加历史 OpenAI 7/7 API 调用。

| 故障 / 场景 | 结果边界 |
|---|---|
| 相同 / 冲突 closeout、并发相同命令 | 一个提交，其余明确 replay；同 ID 改参数拒绝 |
| streaming / tool in flight / unknown dispatch | closeout 拒绝，安全提交点前不记录完成 |
| 旧 consumed cursor 落后 head | 完整补 delta 后再推进，未消费事件与新消息不跳过 |
| 重复 / 错接 / 不前进 / 类型错误页、后续页网络中断 | 整体拒绝或回滚；重试不重复消息引用和计数 |
| 提交前失败 / 提交已成功但返回丢失 | 前者无半记录；后者同 ID 读取已提交回执；激活丢回执不重读已记账的 window receipt 或重做外部动作，但仍核对当前边界 |
| unknown / failed / 伪绑定 / 布尔冒充 epoch 的回执 | 不激活、不显示用户回复完成 |
| 回执 confirmed 但当前 runtime 未匹配 / 已切去别的窗口 | 拒绝旧回执覆盖当前运行状态 |
| 新输入、修订、ACL撤回、版本切换、provider变更、会话删除 | 重新补齐/核验或明确拒绝，不能沿用旧 capsule |
| pending 无 settlement 消失、坏持久 epoch/phase、CAS 冲突 | 失败关闭，不静默丢 pending 或覆盖并发状态 |
| 新进程重开相同 canonical 文件 | 恢复 prepared 和 pending，第二次请求 replay，逻辑聊天 ID 不变 |
| API 与 Codex 标签夹具 | 两组模拟观察均跑通同一协议；**不等于两条真实 provider 路径已接入** |
| 源正文 / canonical messages / session rows | lifecycle 持久记录无源正文；消息行与用户聊天数不增加 |

全仓回归以本轮 Draft PR 最终 commit 的 CI 为准；本地未宣称运行全部后端集成测试。

## 6. 尚未完成的上线门槛与下一断点

| 项目 | 当前状态 | 下一步 |
|---|---|---|
| 纯生命周期协议/状态机 | 本轮离线通过 | 审查本候选；尚未合并 |
| durable repository | 仅测试适配器在合成 canonical DB 中实现 | GM-02B 设计真正复用 relay/message 库的操作表、codec、迁移/回滚与幂等事务；不另造用户会话库 |
| 配置文件 + SQLite + runtime + G3 的 common fence | **真实保证未建立** | 审查所有 writer/跨进程竞争、authority revision/锁与失败恢复；测试 surrogate 不能代替 |
| API / Codex runtime 观察与 capsule 应用 | **真实均未支持** | 各自提供可信当前窗口、实际 consumed cursors 和完整确认回执；未接入路径明确拒绝 |
| 模型 usage 与安全触发 | 无接入、无自动动作 | 真实观测可靠后才评估触发阈值；流式/工具过程中等待安全点 |
| GM-00C 真实持久备份/恢复 | 未执行 | 私有独立故障域、密钥取回、授权及隔离恢复验证 |
| GM-01B 真实 G3 只读接入 | 未验收 | 权威 source/event/revision、ACL/撤销/分页一致性 |

下一项是 **GM-02B 的真实后端适配设计与非生产验证**，先核对当前多存储事务边界和 writer；
不跳过生命周期而直接宣布 GM-03 召回/GM-04 浮现已可上线。
本轮不执行生产 schema/配置变化、服务重启、Caddy 80/443、外部模型调用、G3 写入、自动记忆写入、
新建聊天或消息发送。商业 C 不进入日常链路；独立可信 C 下界仍未建立；生产 **HOLD**。
