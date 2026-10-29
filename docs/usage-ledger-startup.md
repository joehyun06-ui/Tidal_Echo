# 账本候选接线、启停与回滚

本批已在候选分支接入默认关闭的入口，**尚未合并、部署或更改线上设置**。没有读取生产
配置、账户、聊天或独立记忆验收内容，没有真实模型请求。v48 与 36/36、108/108 门槛
保持冻结；下述示例是待审阅的装配位置，不是已执行的部署。

## 启动模式

| `usage_ledger_startup.prepare` 的 mode | 行为 |
|---|---|
| `disabled`（默认） | 返回 None；不检查服务配置/密钥，不打开账本，不创建采集器或挂载端点。已有文件不会删除 |
| `history` | 不允许携带采集配置，不创建模型用量采集器。保留读取、设置、删除和既有回执核对；留下两种模式的主动停采标记 |
| `capture` | 必须明确配置至少一种采集器，复用已有不可变配置和 runtime kwargs；本函数不启动 runtime 或发请求 |

history/capture 需传入有效 UUID `activation_id` 与显式 `service_options`。停采标记按
activation UUID 去重；重开 capture 不删除旧标记。标记只证明发生了显式停采选择，
不是精确停采时长、丢失请求数或费用。未记录的消耗保持未知，相关模式预算剩余不可完整
计算；会话/模型筛选不能藏掉这个范围。页面把主动停采与错误计数分开显示。

capture 若仅配置 API 或仅配置 Codex，另一模式也保留该次启动未采集的范围标记。它不
表示此前一定启用过或实际丢失了请求；用于防止切换成单模式采集后，另一模式的历史
预算仍被误认为覆盖完整。

这不是实时开关。不要在运行中的 runtime 上替换采集器。已在途的旧请求仍可按原快照
结束；进程退出造成的未确认标记保留，不重发模型请求。完全回滚到 disabled 不会自行
记录未运行期间的事实；需要停采审计时，先在受控切换中选择 history。

## 接线选择

会话权威、选中消息版本、续窗工厂和创建/删除锁都归 loop 所有。PWA 的账本请求先由
relay 鉴权，再交给 loop 校验内部凭据和会话归属。loop 从独立 SQLite 读取账本。

relay 不读取来源库，不另造会话权威对象。已有独立实例/子进程测试继续保留，但真实
relay 的读取路径由 `usage_ledger_proxy.install` 转发给 loop；模型用量仍由 loop runtime
构造时显式注入。两个公开/内部前缀只接受 `/app` 或 `/loop`，不接受浏览器指定目标。

候选分支已落地的安装位置：

1. `examples/api_loop_codex_canary.py` 创建 runtime 前调用 `load_loop`，缺省返回 None。
   只有返回服务对象时，才向现有 runtime 构造器展开对应 kwargs；history 不创建模型
   用量采集器，但仍显式接入手动压缩回执，不把上下文变化当作 API 消耗。
2. 既有 integration 建立后，绑定其 `session_authority`、`_session_lock` 和
   `create_web_session`。为原上下文动作安装器提供 `action_kwargs()`；普通会话删除提供
   `usage_ledger` 钩子，版本删除安装器展开 `deletion_kwargs()`。不能重复安装旧路由。
3. loop 在父级 legacy app 挂载前执行：
   `services.install_reader(loop_app, check_auth=legacy.check_internal_auth, prefix='/loop')`。
   这段代码已在候选入口中按服务对象是否存在分支安装。
4. `backend/p3_codex_relay_app.py` 调用 `install_relay`，缺省立即返回 False。选择启用时，
   使用既有 `relay.check_auth`、数字回环 origin 和服务端内部 token；不会读取或转发
   浏览器通行凭据。只有该适配器显式调用底层 `usage_ledger_proxy.install(enabled=True)`。
5. 真实部署仍需核对持久路径、权限、账户/线程归属、CLI 版本和完整主/备用路由清单，
   以及确认入口选择与挂载顺序；本批没有代做这些线上检查。

`service_options` 沿用组合层字段；不接受任意 API Key 等额外字段。HMAC 密钥与内部
token 只能由服务器运行时取得，不能写入 JSON 计划、仓库、模板、报告或日志。

## 入口配置契约

以下名称只在候选代码中定义，本轮没有向线上环境写入任何值。

| 环境变量 | 行为 |
|---|---|
| `GUITING_LEDGER_MODE` | 缺省为 disabled；只接受 disabled / history / capture，拼写不合法返回固定启动错误 |
| `GUITING_LEDGER_PLAN` | 仅非 disabled 时读取：受信持久根目录内的绝对 `.json` 路径，禁止符号链接，最多 64 KiB；沿用离线计划结构，拒绝重复键和凭据字段 |
| `GUITING_LEDGER_ACTIVATION_ID` | history/capture 的 UUID；history 的主动停采标记据此去重 |
| `GUITING_LEDGER_CONTINUATION_KEY` | 只在非 disabled 时从运行时读取，32–512 个可见 ASCII 字符；跨实例/重启需保持稳定 |
| `GUITING_LEDGER_FINGERPRINT_KEY` | 仅 API capture 读取，约束同上；history / Codex-only 不读取 |

两项密钥也可分别通过同名加 `_FILE` 的变量指定服务器私有文件，例如
`GUITING_LEDGER_CONTINUATION_KEY_FILE`。只选择环境值或文件路径之一；同时提供会
拒绝启动，不猜测优先级。文件必须位于已配置的受信持久根目录内，所有下级目录仅限
当前服务用户访问；文件须由该用户所有、权限不开放给组或其他人、无硬链接别名、为常规
文件，内容严格为 32–512 字节可见 ASCII，不带换行。逐层目录描述符与 no-follow 打开
拒绝符号链接；FIFO 等非常规目标不会阻塞启动。此文件模式只支持具备必要 POSIX
文件系统保证的平台，其他平台明确拒绝；原有环境变量方式保持可用。

密钥可在服务器内生成并保存，配置只传路径。该机制不会生成、轮换、复制、打印或上传
密钥，不提供读取密钥的 HTTP 接口。持久盘及私有文件不是加密秘密管理系统；备份、
主机访问控制与密钥生命周期仍须管理。disabled 不检查任何密钥路径；history 或
Codex-only 不读取 API 指纹文件。启用前使用隔离账本完成备份/恢复核对，并确认在途工作。

入口验证计划中的根目录、relay/native 来源路径与现有部署配置一致，账本仍由组合层
检查独立路径，涵盖 SQLite 主文件及 WAL、SHM、rollback journal 的名称和已知别名。
已存在的辅助文件必须是独立常规文件；这些启动时元数据不证明物理持久化或避免路径竞态。
capture 要求计划声明一致；声明不能替代线上归属、存储和通知连续性验收。
relay 复用现有内部 token，不接受新的浏览器凭据或任意转发目标。

显式启用后配置/存储不合法会以固定类别拒绝启动，不静默猜测费率或假装采集成功。
disabled 路径只检查 mode，不读取计划、相关密钥或打开账本。真实入口的隔离导入测试
覆盖三种模式，原有关闭态入口导入检查继续执行，并验证没有新增账本路由。

普通删除和消息版本删除均注入同一账本。普通删除若聊天已删除、账本清理失败，会返回
503 和 `deletion_unconfirmed=true`，不泄露存储错误；同一删除可重试核对。disabled 保留
原异常路径。跨存储仍不是分布式事务。

## relay 转发契约

- 只安装原账本的汇总、25 条默认分页、JSON/CSV 导出与设置四类路由。仅转发受限的
  查询参数；设置最多 8 KiB，校验原 policy、revision 与重复 JSON 键。
- 目标限定显式端口的 `http://127.0.0.1` / `http://[::1]`，不接受 DNS、外部地址、路径、
  用户信息或查询。这个限制针对当前同机 relay/loop 架构；远端 loop 需要另行审阅。
- 服务端使用内部鉴权头；不转发浏览器 Authorization、Cookie、Origin。HTTPX 禁用
  环境代理与重定向，不重试。转发失败只给固定错误，不回显 URL、token 或上游错误正文。
- 普通 JSON 限 8 MiB，并核对会话、模式和范围。导出分块转发，没有按页/固定条数截断，
  不缓冲整个文件。只接受对应 JSON/CSV 内容类型，不转发上游 Cookie/重定向等头。
- 断连、首个响应头发送失败、尚未开始消费或中途错误都会关闭内部响应/客户端。中途
  错误中止旧流，不补造完整文件，不重试导出。前端沿用原导出失败提示。
- 设置若超时可能已被保存：需刷新核对 revision，不能自动重放。保留 409 CAS 冲突。
- 所有代理成功/错误响应为 private/no-store。内部鉴权失败不冒充浏览器登录失效。

账本导出仍是已保留的请求明细，不是账户账单；主动停采证据在 journal 和页面中保留，
不能从 CSV 请求行推断全部采集历史。

## 停采、恢复与回滚顺序

1. 准备 history 配置：同一账本/来源路径、同一续窗密钥、新 activation UUID，省略采集
   配置。先核对停采后的公开读取/删除路径仍安装，再进行受控进程切换。
2. 使用现有 runtime 的关闭流程处理在途工作；不承诺无损排空或上游停止计费。未确认
   状态保留，不能根据重启把它改成零消耗或失败。不要启动第二套模型工作循环。
3. history 模式保留统计、不可变价格和删除标记。暂停期间的生成沿用原业务流程，但
   没有模型用量采集；回执核对和消息计数不等于补回缺失消耗。
   API 采集器附带的缓存参数也不再注入，回到原提供方默认行为，不能承诺停采前后的命中率。
4. 恢复时审阅新 profile，再以 capture 重建。Codex 首轮无可靠基线时保持不可用，不能
   以线程累计量填补停采期间的消耗。旧停采/异常证据继续保留。
5. 若撤回整个功能，选择 disabled 并回到未安装代理/读取端点的原组合，保留独立账本
   文件和受管备份。不要删除库、重置游标密钥或重算历史价格。原生产分支目前仍是此状态。

## 离线验证

```bash
python -m unittest backend.tests.test_usage_ledger_startup backend.tests.test_usage_ledger_proxy
```

测试涵盖默认无操作、capture/history/恢复、预算未知、删除与迟到完成、真实两层 ASGI
鉴权、分页/导出/设置、错误与重定向不泄露、超时不重试、JSON 上限、流式背压和断连
资源释放。前端 fixture 通过同一真实代理访问独立 loop app，HTTPX 传输为 ASGI Mock，
Python 出站网络与浏览器外部请求仍被禁止。真实部署尚未执行。

入口与配置的新增验证：

```bash
python -m unittest backend.tests.test_usage_ledger_entrypoint
```

隔离子进程使用临时计划、合成密钥和关闭的模型运行开关，禁止出站连接。它导入真实
入口代码并检查三种模式的挂载与注入，不启动 SDK 工作循环、账号登录或真实生成。
