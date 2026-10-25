# 账本候选接线、启停与回滚

本批提供候选安装函数并用离线 Mock 验证，**没有改动或启用正式入口**。没有读取生产
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

这不是实时开关。不要在运行中的 runtime 上替换采集器。已在途的旧请求仍可按原快照
结束；进程退出造成的未确认标记保留，不重发模型请求。完全回滚到 disabled 不会自行
记录未运行期间的事实；需要停采审计时，先在受控切换中选择 history。

## 接线选择

会话权威、选中消息版本、续窗工厂和创建/删除锁都归 loop 所有。PWA 的账本请求先由
relay 鉴权，再交给 loop 校验内部凭据和会话归属。loop 从独立 SQLite 读取账本。

relay 不读取来源库，不另造会话权威对象。已有独立实例/子进程测试继续保留，但真实
relay 的读取路径由 `usage_ledger_proxy.install` 转发给 loop；模型用量仍由 loop runtime
构造时显式注入。两个公开/内部前缀只接受 `/app` 或 `/loop`，不接受浏览器指定目标。

具体待审阅的安装位置：

1. 在创建 runtime 前调用 `prepare`，默认保持 disabled。只有返回服务对象时，才向现有
   `ApiWebRuntime` / `StreamingCodexGenerationRuntime` 构造器展开对应 kwargs。
2. 既有 integration 建立后，绑定其 `session_authority`、`_session_lock` 和
   `create_web_session`。为原上下文动作安装器提供 `action_kwargs()`；普通会话删除提供
   `usage_ledger` 钩子，版本删除安装器展开 `deletion_kwargs()`。不能重复安装旧路由。
3. loop 在父级 legacy app 挂载前执行：
   `services.install_reader(loop_app, check_auth=legacy.check_internal_auth, prefix='/loop')`。
   调用为显式安装，原入口当前没有这段代码。
4. relay 在既有总应用的路由层显式执行 `usage_ledger_proxy.install`。默认
   `enabled=False` 完全无操作；候选启用时，提供 `relay.check_auth`、受审的数字回环
   origin、服务端已有内部 token。不能把浏览器通行凭据当作内部 token。
5. 真实部署仍需核对持久路径、权限、账户/线程归属、CLI 版本和完整主/备用路由清单，
   以及确认入口选择与挂载顺序；本批没有代做这些线上检查。

`service_options` 沿用组合层字段；不接受任意 API Key 等额外字段。HMAC 密钥与内部
token 只能由服务器运行时取得，不能写入 JSON 计划、仓库、模板、报告或日志。

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
