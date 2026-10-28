# 归汀 · Uvicorn 转发头自动重写禁用（离线安全验收）

堆叠 Draft #156（21/21 工作流，40/40 CI 任务；Linux 全量 2007/2007）。本阶段只调整 `scripts/render_start.py` 中两个子进程的 Uvicorn 显式启动参数，在 `api_loop` 和 `relay` 各增加 **`--no-proxy-headers`**，不添加真实 OIDC、登录、记忆写入端点、不读取真实用户或生产密钥、不改变 `render.yaml`，不部署。所有新功能仍默认 OFF。

## 为什么需要这一修改

Uvicorn 可依据 `forwarded_allow_ips` 和 `ProxyHeadersMiddleware` 将请求的 `X-Forwarded-For` / `X-Forwarded-Proto` 解释为 `scope.client` / `scope.scheme`；此前 Render supervisor 的命令未显式给出 `--no-proxy-headers`。若把已经规范化的 `request.client` 当作原始 TCP 对端，可能错误提升客户端可控头部的权限。本次明确关闭 **Uvicorn 这一层** 的自动重写；它不清除/净化原始 HTTP 转发头，也不提供真实来源证明。

## 验收方式

- Linux、Windows 分别运行 `backend.tests._memory_oidc_uvicorn_proxy_offline_worker`。
- 验证两个子进程命令保留原监听地址（内部 API loop 为 127.0.0.1；外部 relay 为 0.0.0.0）、端口、单 worker 和关闭访问日志，且明确包含唯一的 `--no-proxy-headers`。
- 通过真实 Uvicorn Click CLI 参数解析（拦截实际启动函数，**绝不启动服务**）确认选项 `proxy_headers=False`；即使 `FORWARDED_ALLOW_IPS=*` 也不因此启用重写。
- 加载真实 Uvicorn ASGI app stack（**无 TCP listener**）：带有伪造 `X-Forwarded-For` / `X-Forwarded-Proto`、重复和混合的转发头时，关闭中间件会保留未解释的 `scope.client`、HTTP `scope.scheme` 以及原始头部列表。
- 对照试验启用该中间件并使用宽松信任来源，证明同样的合成头可能改写 `scope.client` / `scope.scheme`；因此显式禁用有实质意义。
- 测试禁止真实网络、真实身份提供商、付费 API 和记忆写入。仅检查 Uvicorn 启动配置和进程内行为。

## 不可宣称为生产可信代理验收

1. **没有真实 Render ingress 可信头清洗证明。** Render 网络层、代理源 IP / ACL、HTTPS 终止及 XFF 来源真实性未在本阶段验证；真实服务中是否存在上游代理或内部访问路径仍须独立审计。
2. 关闭 Uvicorn 自动解析并不自动解决 `Host` 注入、不同负载均衡层的头部拼接、ASGI 前置中间件修改、直接访问、Forwarded 多跳或其它来源混淆。#156 的 `raw_socket_peer_provider` 和 `ingress_header_sanitization_proof` 依然只是测试桩，尚不能授权。
3. 由于原有 relay 可监听 `0.0.0.0`，本轮**不**声称内部服务不可直连；正式上线前必须核对 Render 实际拓扑与来源过滤。
4. 正式启用前还须验证现有经 TLS 终止的正常浏览器请求是否依赖自动重写后的 `request.url.scheme` / `request.client`；本 PR 未验证生产兼容性，不能据离线 CI 批准部署。

所有 PR 保留 Draft、未合并、未部署；真实 IdP、用户登录、真人同意、记忆写入及付费 API 均未启用。v48 正式冻结验收门槛 **36/36、108/108** 不变。
