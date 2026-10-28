# 归汀 · 服务端登录纪元注册表（仅离线 Draft）

**范围：** 叠加在后端 Draft PR #137 之上，不合并、不部署、不接真实身份、不开记忆保存接口，不更动 v48 冻结验收基线、付费 API 账本或前端协议。独立模块 `backend/memory_auth_epoch_store.py`；环境变量 `MEMORY_TRUSTED_AUTH_EPOCH_ENABLED` 默认关闭（仅字面 `true` 打开）。

## 设计边界

- `AuthenticatedLogin(principal_id, login_id)` 必须由**已验证登录的服务端身份组件**生成；Python dataclass 并不构成真实认证、权限证明或浏览器 token。任何客户端 JSON、Cookie 未验证字段、URL 或 Header 都不得用来构造可信主体。
- `issue_after_authenticated_login()` 仅由**确实成功完成登录/重新登录的可信回调**调用，返回 `ServerIssuedEpoch`，由服务端认证会话安全保存。**该纪元不能作为登录 Bearer 凭据，也不能传到前端。**
- SQLite 保留 `principal_id + login_id`、SHA-256 纪元摘要、单调递增 generation、撤销标志和时间戳，不持久化纪元明文；同一登录重新签发即原子轮换。每次校验使用 SQLite 只读连接；多工作进程共享同一文件能看到已提交的轮换/撤销。
- `revoke_before_logout()` 必须在清除真实登录上下文前调用；注销所有设备时使用 `revoke_all_for_authenticated_principal()`。不能把“浏览器自行删除 Cookie”视作成功撤销。
- `make_confirmation_provider()` 只是一个将现有 `AuthorizedSessionContext` 接入**服务器校验过的**纪元的离线样例，不自动接入任何 HTTP 路由、生产服务或真实数据库。所有回调必须绑定同一受信任请求的主体、登录和会话，不能从用户请求参数声明授权。
- 现有内存确认票据仍是**进程内、不持久化**；当前该注册表与原有 SQLite 记忆写入使用两个不同事务，**校验之后、实际记忆提交之前**仍存在撤销并发竞态。生产必须补充一致性锁定/事务协调、真实登录/CSRF、跨进程票据 claim、故障恢复与用户同意证明，才能启用任何写入口。

## 验证

- Linux/Windows：`python -m unittest backend.tests._memory_auth_epoch_worker -v`
- 合成身份覆盖：默认关闭零副作用；后端签发；多实例共享；重新登录旧纪元失效；退出登录撤销；切换主体/登录；撤销全部；错误上下文、伪造纪元安全失败；并发轮换；旧确认票据在退出/重新登录后拒绝且模拟服务零写入。
- 该测试可在内存模拟服务上临时打开原有确认开关，但不触碰真实用户数据、真实认证、网络、生产数据库或付费 API。
