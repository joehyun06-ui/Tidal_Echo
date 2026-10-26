# Codex 通知连续性的离线传输验收

本批让合成 Python 子进程通过真实 stdin/stdout JSONL、共享传输 reader、原运行时
账本回调和 CodexCollector 进入临时 SQLite。这条 JSONL 链路使用合成 Python 子进程，
父/子进程均禁止网络连接。另用实际固定版本 CLI 生成协议 schema，见下节。没有读取
真实账号认证文件、登录、读取生产数据或调用模型。v48、36/36、108/108 保持冻结，
仍在独立草稿中，生产采集继续关闭。

## 修复的差值归属问题

已入账的上一轮累计计数可能随后收到不同的迟到观测。若下一轮已经开始，它已保存
上一轮的引用并移除了 thread head；仅删除 head 不能阻止它继续用旧值计算单轮差额。
无效用量也有同样问题，且原来没有增加 pending revision，磁盘核对可能提交旧快照。

现在在同一采集器锁下作废该线程的 head、待核对轮次及已保留 checkpoint，同时增加
待核对 revision。已开始的后继轮次不能使用旧值；核对时发现 revision 变化就放弃该次
内存快照，留待下一次核对。受影响轮次保留原始观测，但单轮用量为不可用，不填零。
健康的后续完整边界可以重新建立基线。其他线程的连续差值不受影响。

不改写已经落库的历史观测。迟到/无效用量仍以固定类别记入持久化采集异常；异常可在
“会话选项 → 用量账本”查看，不能据此宣称已入账历史或账户总用量完整。这不是新增
跨数据库事务或断电时原子提交的保证。

## 验证场景

| 场景 | 预期结果 |
|---|---|
| 通知早于持久化 ACK 身份 | 只暂存，精确 thread/turn/message 绑定出现后才入账 |
| 多次模型请求与重复通知 | 一个终态记录，按线程累计差值，不能相加重复 last 快照 |
| 子进程 EOF 与显式重连 | owner 丢失改变 epoch，不自动重试模型；新边界首轮不可用 |
| 非法 JSONL / 重复键 | 传输终止旧 owner；不能跨 epoch 接续差值 |
| 新观察者收到旧轮重放 | 历史幂等，不以重放计数作为新运行时基线 |
| 持久轮次存在、通知缺失 | 不越过缺口推算单轮用量 |
| 后继已开始后前轮用量变化 | 作废后继基线，保留旧入账记录并登记 late_event |
| 前轮迟到 payload 无效 | 作废后继基线，固定 invalid_usage，不保存私密字段 |
| 无效用量与磁盘核对并发 | revision 变化阻止提交已过期的暂存快照 |
| 同时观测其他独立线程 | 异常只影响对应线程，健康线程继续计算 |
| 其他通知消费者抛错 | 共享 reader 隔离异常，账本消费者仍收到通知 |

11 项新增检查中，迟到归属和并发核对相关的 4 项在修复前失败，修复后通过。专项命令：

```bash
python -W error::ResourceWarning -m unittest backend.tests.test_usage_ledger_native_transport
```

测试子进程仅认识内部 `fixture/*` 方法，使用临时 home 和来源库。它不实现 Codex 推理，
不会调用 `turn/start`、`thread/resume` 或订阅账户接口。该合成链路本身不构成对实际
CLI、正式账号或服务端通知行为的验收；协议字段另按下节核对。

## 实际 CLI 生成的协议校验

复用 `test_codex_0160_contract` 的真实 CLI 验收基础，固定 wheel 和二进制版本均为
0.160.0。版本读取和 `app-server generate-json-schema --experimental` 仅在临时目录
执行，显式提供空的 HOME/CODEX_HOME 与最小子进程环境，不继承真实配置、认证文件
或父进程密钥。已有的匿名 initialize/account/config 检查继续保留，不发起登录或推理。

新增 4 项账本协议检查，同时验证实际生成的 ServerNotification envelope 与 v2 参数
schema，再把通过校验的合成通知交给真实采集器和临时 SQLite：

| 场景 | 核对内容 |
|---|---|
| JSONL 合成样例与单轮差值 | 轮次通知补齐必需的 `items`，精确绑定和输入/输出/推理差值符合固定协议 |
| 空上下文上限与原生缓存字段 | `modelContextWindow` 缺省/null 保持不可用；缓存字段保留原文，Codex API 缓存/费用仍不适用；真实报告的零差值保留零 |
| 压缩生命周期 | `contextCompaction` 和必需时间戳通过 schema，阻断压缩前后差值，保留前后观测且记忆保留未知 |
| completed/failed/interrupted | 原生终态与错误结构通过协议核对，记录状态但不把合成回复或错误正文写入账本 |

命令：

```bash
python -W error::ResourceWarning -m unittest backend.tests.test_codex_0160_contract
```

这些 schema 每次由安装的真实 CLI 重新生成，不以工作区旧副本作为 CI 依据。没有
增加模型请求、SDK 采集、登录步骤或生产启用选项。协议通过只证明给定合成消息符合
这一版本的字段契约，不证明真实通知顺序、断线恢复、账户归属或完整用量。

依据：[OpenAI Docs · App Server message schema](https://developers.openai.com/codex/app-server#message-schema)。
官方说明生成产物与执行生成命令的 CLI 版本对应；升级时应重新核对，不放宽现有门槛。

## 子进程关闭与回收

实际 CLI 与合成传输的组合检查暴露了资源清理警告。本批另修复共享传输关闭时的
4 个可复现边界：取消 reader 后等待其清理；终止超时后 kill 并再次 wait 回收；
reader 已摘除当前 owner 时 close 仍等待在途清理；失败调用方取消不会取消独立清理。
对应 4 项回归在修复前失败，修复后通过。

清理任务单独持有旧 owner，由 runtime 跟踪并在 close 时等待；完成后释放跟踪引用。
终止/回收和 reader 收尾沿用有期限的等待，不把“发出 kill”当成“回收完成”。这不是
无限期限下保证清理成功，也不新增模型请求、重放、登录或自动部署。账号、协议方法
权限、进程启动参数和账本启用条件均保持原规则。

## 尚未验证

实际 Codex CLI 的通知顺序、服务重启后的真实订阅恢复、外部客户端对线程的独占、
真实账户归属和生产数据备份恢复仍需独立验收。没有创建第二套线上 loop、切换模型、
修改环境变量或开启采集；本测试通过不会触发部署或真实模型请求。
