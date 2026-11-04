# 归汀 GM-02B：持久仓库、独立 JSON 配置适配与提交边界

日期：2026-11-04。依据 v1.1，接续 GM-02A。

**本轮结果：28 项新增非生产适配测试、116 项既有回归通过。SQLite 仓库候选可用；跨 JSON/SQLite/runtime/G3 的共同提交保证仍未建立，生产 HOLD。**

## 1. 基线与交付

固定父提交：`5eadc05d7ea1dfba3520ea709493ffbb11988127`（Draft PR #202）。
新分支基于 #202，PR 指向其开发分支；不修改或合并此前草稿。生产目标分支本轮读取仍为
`c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4`；没有重新实测线上部署或磁盘。

| 新文件 | 作用 |
|---|---|
| `backend/session_lifecycle_codec.py` | 严格、版本化、有界 JSON codec；恢复 dataclass/tuple；检查状态、回执、来源引用及游标类型 |
| `backend/session_lifecycle_storage.py` | 默认关闭的 canonical SQLite 仓库、私有 JSON 只读适配、显式候选 schema 安装/空表回滚 |
| `backend/tests/test_session_lifecycle_storage.py` | 真实消息表 DDL/连接、独立 JSON 文件、真实 API Store 写入器及新进程恢复测试；只用 synthetic 数据 |
| 本文档 | writer 核对、迁移/故障边界与下一断点 |

没有修改现有运行入口、配置、会话/消息 writer、Journal/WAL、依赖或 G3 冻结账本。

## 2. 已核对的 writer 边界

以下是审阅覆盖范围，不是全仓所有 writer 已被穷尽或生产安装状态已确认的声明。

| 来源 / 方法 | 存储与现有机制 | 对生命周期的含义 |
|---|---|---|
| `examples/api_loop.load_config/save_config` | 读 JSON；通过 `deployment_config.atomic_write_text` 写文件 | 原子文件替换不是跨存储事务 |
| `deployment_config.atomic_write_text` | 新临时文件、0600、文件 fsync、replace、父目录 fsync | 防止部分文件发布；不提供读改写 CAS 或共同进程锁 |
| `WebSessionProviderAuthority.save_sessions` | load_config → 更新 session 元数据 → save_config | 会话/provider 继续复用现有权威，不迁入第二套会话库 |
| `CodexCanaryLoopIntegration.install_legacy_globals` | 安装时为部分 session 入口使用实例 `RLock` | 锁只在该进程/实例内；不能据此证明全部配置 writer 或其他进程受同一锁约束 |
| `examples/api_loop.update_config` | 另一个 load/modify/save 路径 | 在已审代码中没有证明它与上述实例锁共享同一跨进程协议 |
| `api_web_generation.Store.db/accept` | relay 库 `BEGIN IMMEDIATE`；canonical input 与任务原子写入 | 本轮实际调用此 writer，证实会被同库仓库事务阻塞；不派发任务或调用模型 |
| `web_message_versions.select_version` | 在 relay SQLite 中更新 selected version | 同库事务期间稳定；后续 load 必须拒绝选中版本已变的旧状态 |
| `web_session_delete`、Codex generation/compaction store | 存在分步 JSON/SQLite/运行时操作及独立连接 | 必须继续纳入 writer 和实际路径盘点，不能默认处于同一事务或同一物理库 |

已读源码均固定父提交。关键 blob：channel_store `ae8e612bc027b58e67c093582d4bcac43b7b1ba2`；
api_loop `baa6fcf624d025cdc97cfaab5d6d8c15d8fa8c75`；deployment_config
`0a7746d35f3e888ea6cfb10d4faa94e2816d4d16`；canary integration
`2f2e9d61badbed621e22eee0657a0a5a4828fe6f`；API writer `4ae12c50bfab715fbfb3936cdcf125ce2415f86c`。

## 3. 仓库与 codec

仓库接收调用方提供的**既有 canonical 连接工厂**，不读取数据库路径或环境变量、不自动建库或安装表。
默认 `enabled=False`，调用即拒绝且不打开连接。工厂仍负责正确路径、权限、既有文件和受控连接配置；
schema 检查不能反过来证明工厂打开了正确的生产挂载。本轮工厂只指向临时合成库。

候选扩展仅有 `session_lifecycle_meta/records/receipts` 三表，与消息共用一个文件；
不修改 `messages`，不推进现有 `schema_migrations`，不存第二套用户会话。安装必须显式调用，
先验证真实 relay 消息列和 foreign_keys；已有调用方事务会被拒绝，不能顺手提交其他工作。
未知版本、部分 schema、冲突表、额外 owned trigger/index 均拒绝。

状态与幂等回执在一次 SQLite 事务内保存，以 expected revision 做 CAS。回执不能先于状态成为可见成功。
回滚函数只允许移除**空**候选表；已有记录时拒绝，不提供强制丢弃 pending/历史回执的选项。
真实迁移协调、数据备份、已有数据处置及生产回滚仍需单独审阅和授权。

codec 使用明确版本和精确字段集合，拒绝重复 JSON key、NaN、布尔冒充整数、未知版本、错误 phase、
错序消息引用和非法 source ref。源文本不自动复制；只保留引用和既有派生候选。编码上限 256 KiB，
SQL 先限制类型/长度，再把 payload 交给 Python decoder，防止坏记录形成无界读取。
SHA-256 只检测意外破坏；**不是认证签名，也不防持有数据库写权限的人重写哈希或回滚整库**。

每次 load/save 还核对：当前 JSON 中 root/provider 仍存在；当前 selected version 与记录一致；
version edge 属于原 root；canonical message cursor/refs 未删除或转到别的 session。
内部 alternative 不能被当成新的 lifecycle conversation root。事务对象在退出后不可继续使用。

## 4. 独立 JSON 文件适配

`JsonSessionReader` 在显式路径上只读打开现有私有普通文件，限制体积、owner/mode，拒绝叶子 symlink，
严格解析 JSON，并通过真实 `WebSessionProviderAuthority` 规范化 session/provider/tombstone。
未知或已删除 session 不使用“缺省 API”兜底。原有历史 provider 回调仍由可信后端提供，
其独立数据源的一致性尚未解决；测试使用合成回调。

返回的 view 只含必要 session/provider 标识和文件 identity/hash；不返回 route key、完整配置或聊天正文。
目录由可信调用方控制；没有声称对恶意文件系统竞态或祖先目录替换作了完整加固。

事务提交前检查 JSON 版本，已观察到变化则回滚 SQL；提交后再检查，若变化则明确返回
`lifecycle_authority_changed_after_commit`，此时状态/回执可能已经提交，须用同 request ID 核对，不能盲重发。

**这些检查仍不是共同锁。**测试让一个不遵守协议的 writer 在最后一次文件观察后替换 JSON：
操作仍可能返回，但配置已改变。这是受控的边界反例，不是跨存储安全验收通过，亦不声称生产已发生此故障。
代码因此明确保留 `cross_store_atomic=False`、`production_supported=False`；没有把它安装进运行时。

## 5. 非生产验证

测试复用真实 `channel_store.RELAY_TABLE_DDL['messages']`、`channel_store.connect`、
`deployment_config.atomic_write_text`、`WebSessionProviderAuthority` 及 `api_web_generation.Store.accept`。
相比 GM-02A 的单文件 legacy surrogate，本轮 session 配置是独立 JSON 文件，操作状态仍与 canonical
消息共用 relay 文件。runtime、G3 与身份上下文仍是 synthetic；这不是生产数据恢复。

```bash
python -m unittest \
  backend.tests.test_session_lifecycle_storage \
  backend.tests.test_session_lifecycle \
  backend.tests.test_g3_projection \
  backend.tests.test_g3_projection_feed
```

本地 Python 3.12.14：**新增 28/28 + 既有 116/116 = 144/144 通过**。新 Python 文件语法解析通过；
源快照及上传源码核对 Git blob SHA。完整仓库测试以最终 Draft PR CI 为准。

| 验证组 | 已取得的证据 |
|---|---|
| 仓库恢复 | 原状态机通过真实仓库完成 closeout/reentry/确认；新 Python 进程重开独立 JSON + 同一 relay 文件；同 ID replay 保留原聊天和 pending |
| SQL 事务 | 状态写入后、回执写入前注入失败，两者共同回滚；提交已成功但回执丢失时只核对同 ID |
| schema/回滚 | 显式安装可重复；空表回滚不碰消息；有数据拒绝 DROP；owned trigger、未知版本和错误 relay 拒绝 |
| 数据/资源 | payload/hash/回执污染、未知 codec、重复 key、非法类型、超长记录在解码前拒绝 |
| 引用与身份 | provider/selected version 改变、内部 alternative 冒充 root、消息删除或跨会话引用均拒绝 |
| 配置边界 | 私有文件、重复 key、symlink、公开权限、未知/删除 session、提交前/提交后原子替换均覆盖；原始密钥不进入 view/状态 |
| 真实 SQL writer | 仓库持有事务时实际 API Store.accept 不能写；释放后可以写入合成输入/任务，未执行模型 |
| 模式 | 仅在临时夹具中验证 DELETE/WAL；仓库不修改两者，生产模式未触碰 |
| 跨文件反例 | 最后观察之后的无共同锁写入仍可逃过检查，说明该保证尚未建立 |

POSIX 私有文件适配只在本轮 Linux 环境验证；跨主机、断电、真实 ACL、真实 provider 消费确认均未测。

## 6. 下一断点与准入

下一项继续 GM-02 的**共同 writer 协议和 runtime 观察适配**：覆盖全部相关 JSON writer、
多进程/锁顺序、持久代数与未完成提交恢复；分别核对 API/Codex 的真实当前窗口、consumed cursors、
stream/tool/unknown 状态与 capsule 确认。仅靠实例 RLock、SQLite 事务或重复文件 hash 不足以替代这些条件。

必须在 non-production 验证协调协议后，才能考虑接入；真实 G3 权威读权限与 GM-00C 持久备份恢复仍是独立上线门槛。
没有新增模型 API、G3/自动记忆写入、用户聊天或消息发送；没有合并、部署、生产迁移、重启或配置变化。
G3-QU 36/36、108/108 和历史 OpenAI 7/7 冻结；商业 C 不进入日常链路；生产 **HOLD**。

参考：固定父提交中的上述源码；[SQLite transactions](https://www.sqlite.org/lang_transaction.html)、
[Python sqlite3](https://docs.python.org/3.12/library/sqlite3.html)。SQLite 的写事务保证作用于该数据库，不自动扩展到 JSON 或外部运行时。
