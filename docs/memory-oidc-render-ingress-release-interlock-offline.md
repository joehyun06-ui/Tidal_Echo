# 归汀 · Render 入口 OIDC / 浏览器身份上线闸门（默认 OFF）

在 Draft #157（22/22 工作流、42/42 CI job、Linux 全回归 2007/2007）基础上堆叠。本阶段只对现有 Render supervisor 入口 `scripts/render_start.py` 添加 **默认拒绝** 的运行时防误开闸门，**不签发会话、不新增路由、不改变 `render.yaml`、不合并、不部署**。

## 安全前提

#156 的 `TrustedOidcProxyProvenanceReview` 只能通过注入的合成代理对端和合成代理清洗回调测试：既没有真实 Render edge 清洗证明，也没有不可篡改、与单个请求绑定的原始 socket peer 证据。#157 显式关闭 Uvicorn 转发头自动重写，**只是消除其中一层隐式信任**；既不证明 Render ingress 已清洗客户端转发头，也不证明真实协议、Host、直接访问和外部身份提供商可信。

## 本次拒绝规则

- `preflight(environ)` **在调用 Telegram 解析、deployment config、持久路径准备和子进程启动之前**检查相关标志位；`child_environment(config, environ)` 再检查一遍，防止绕过预检或在检查后注入开启标志。
- 所有以 `MEMORY_OIDC_` 开头、以 `_ENABLED` / `_APPROVED` 结尾的环境变量（含真实网络显式批准开关）、`MEMORY_PER_USER_AUTH_REVIEW_ENABLED` 与 `MEMORY_BROWSER_CONSENT_REVIEW_ENABLED` 均只允许**未设置**或精确文本 `false`。任何 `true`、`1`、大写、空值、前后空格、非字符串、大小写变化名称的启用尝试均统一拒绝，错误类别固定为 `unreviewed_memory_browser_identity_ingress`，不会记录 Secret。
- 其它非启用参数如 `MEMORY_OIDC_ISSUER` 不被视作开启授权。只保护本 Render supervisor 启动路径，不能替代所有本地/测试入口的隔离。
- 测试验证：已有 Render Blueprint `autoDeploy: false`、入口为 `python scripts/render_start.py`、没有在 Blueprint 打开对应 OIDC 审核标志；两个子进程仍保留 `--no-proxy-headers`。相关判断是**仓库静态配置审查**，并不等价于已部署服务的实时状态。

## 测试与禁止事项

新增 `backend.tests._memory_oidc_render_ingress_interlock_worker`，Linux / Windows 均在纯离线 Mock 条件执行：预检前拒绝、副作用为零的失败、逐标志拒绝、未来新批准开关拒绝、大小写及非规范值、二次子进程环境检查、被绕过预检时不启动子进程、实际仓库 Blueprint 结构检查。未连接 Render、未启动 TCP listener、未连接真实 IdP、未进行真实记忆写入、未调用付费 API。

**生产阻断继续保留：** 确认实际 Render 服务所使用的 commit/分支与 Blueprint 是否一致；审查真实 ingress proxy 的可信对端网络 ACL 和头部先清除再重写的规则；从不可篡改的传输边界取得与每个请求绑定的真实来源；检查 TLS/Host/Origin 与受保护登录回调兼容性；审核真实 IdP 与真人同意证据。若没有上述证据，不得将模拟证明替换成简单的 `lambda: True`，不能通过单独设置某个开关获得上线许可。

继续冻结 v48 正式 **36/36、108/108**，所有安全 PR Draft/未合并、未部署；真实 OIDC 登录与生产记忆写入仍 OFF。
