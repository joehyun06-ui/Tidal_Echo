# GM-02A · 同一逻辑聊天跨窗的合成契约

基线为 #201 的 `4f3f2bd1a0d4fc9533c37ea48a48e73453938363`。模块没有路由、消息写口、provider 客户端或启动钩子。每个显式创建的 SQLite fixture 绑定一个独立 conversation_id 和一个 owner。window_id 属于这个 conversation，不能充当用户身份或逻辑聊天身份。删除、撤权、找不到权威或数据损坏均关闭访问。恢复必须持有原账本与有效 owner binding；消息、会话列表和投影缓存不能重建授权。

## 唯一切换边界

| 阶段 | 唯一 active pointer | 普通发送资格 | 可见证据 |
|---|---|---|---|
| ACTIVE | 初始窗或上次提交的新窗 | 仅该窗，且权限、turn 允许 | genesis 或 final receipt |
| CLOSEOUT_PREPARING | 旧窗 | 请求保留后关闭 | 固定 ID、请求摘要 |
| CLOSEOUT_COMMITTED | 旧窗 | 两窗均关闭 | 不可变 closeout 与 pending/tool 边界 |
| REENTRY_PREPARING / READY | 旧窗 | 两窗均关闭 | 固定 candidate；READY 可读恢复包 |
| ACTIVE_NEW_WINDOW | 新窗 | 仅新窗 | final receipt，epoch 恰好加一 |
| FAILED_PRE_BOUNDARY | 原 pointer | 不解除已经建立的封存 | 固定 target，同请求可继续 |
| UNKNOWN_POST_BOUNDARY | 最后已证实的 pointer | 保守关闭本次交接 | 已持久化 dispatch intent，只读查证 |

`safe_commit_point` 是唯一能写 final receipt 的业务操作。active pointer、context_epoch 和窗口终态从同一不可变提交链推导；没有第二套可独立更新的 active 标志。SQLite 写事务内核验 owner、撤销、旧窗、epoch、版本、closeout、封存、completion、未决发送、pending/tool、唯一候选、包摘要及最新来源权限/修订/游标，随后追加唯一 receipt。事务回滚时 pointer、epoch、终态和 receipt 一起回滚。提交后丢回执时读取同一 receipt。没有 abort/unseal 操作。

每个 closeout、prepare 和 switch 固定 operation/request、目标窗、输入摘要、最后确认阶段与 dispatch/commit 证据。在可能 dispatch 前先持久化 intent。进程中断且缺少进一步证据时，宁可保留 unknown。可证明未 dispatch 的失败允许以相同输入和 candidate 重试。历史回执读取、reconcile、打开文件和重建内存视图只做查询。完成证据不会触发重发。

## 上下文与来源

closeout 保存 state/version、pending、active_state、recent_anchors、derived summary_candidates、tool_boundary、completion、来源游标、时间和摘要。恢复包属于控制数据；不会成为聊天消息。token 压力只能请求 continuation。流式输出、活动工具或不明发送结果阻止切换。预算元数据只记录合成限制，真实 token 使用量未知。

合成事件流采用单一 conversation 的连续正整数游标。每项绑定事件身份、修订及来源摘要。全页按序提交，复读只接受字节一致的重复。乱序、缺口、重复身份冲突、来源撤销、ACL 或 revision 改变时整批回滚。READY 到最终提交之间再次读权威。fixture 来源与生命周期共享 SQLite 写锁，以模拟同一快照；真实 G3 跨存储一致性另行验收。

## 复用与运行路径

| 现有模块 | GM-02A 采用的边界 |
|---|---|
| web_session_provider_authority | 保留已有 provider/native session 权威；显式合成逻辑身份，不调用 publish/activate |
| usage_ledger_creation / continuation | 请求绑定、已过 mutation 边界只读对账、provider 特定 completion 证据；不调用 create/send |
| web_codex_compaction | 准备与 dispatch 分离、busy gate、unknown 不重放；compaction 不充当 closeout |
| web_session_context / continuity_context | 展示与派生上下文不能决定 ownership、发送资格或恢复水位 |
| GM-01A2 projection feed | 采用游标、回源检查和整批失败语义；内存投影不能提供生命周期提交 |

| 路径 | 本轮可证明 | 其余情况 |
|---|---|---|
| API fixture | 已绑定的 completed job 和 canonical reply 一致，upstream_unknown 为 false | 缺失或不匹配为 unknown |
| Codex fixture | native job、canonical reply、generation/client/callback 身份一致 | 缺失 native 能力为 unsupported，证据不足为 unknown |
| 真实 API / Codex | 无运行时适配 | unsupported |

## 持久性与余下门槛

本轮验收措辞为 **process-restart persistence verified in synthetic SQLite fixture**。只覆盖进程退出、重新打开、事务并发与回执找回。断电、文件系统/fsync、真实备份和隔离恢复属于 GM-00C。GM-01B 真实 source/ACL/revision、生产 ownership 与 provider window 映射、真实完成证据和安全发送入口仍待验收。Production HOLD 继续；自动换窗、线上启用及生产就绪均不在本轮。

## 证据的读取方法

`reconcile(target, closeout_id, kind)` 以只读连接返回固定候选、请求摘要、最后确认阶段、是否越界、是否只许对账及已存在的 final receipt。已提交的 closeout 可以按原请求找回。prepare 只在原 epoch 仍是当前 epoch 时返回包。包的 `window_id/context_epoch` 表示拟进入的目标；`previous_window/previous_epoch` 表示尚未切换的旧窗。只有 head 视图给出已提交的路由。

从同一 closeout 补齐一页 delta 会产生新的包版本。旧版本无法覆盖新版本。同一版本若已记录 dispatch intent 且无进一步证据，停止在只读对账；只有能证明本地事务已回滚的尝试才可重新提交。合成来源记录持有事件引用和摘要；fixture 的权限标志和完成证据均属于测试输入。

本地回归包括 web session provider/context/fork、continuity context、usage ledger creation/continuation/compaction 和两组 G3 projection 测试。
