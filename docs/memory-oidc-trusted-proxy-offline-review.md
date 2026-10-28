# OIDC 可信反向代理来源验证（默认 OFF、仅离线）

堆叠 #155。新增 `TrustedOidcProxyProvenanceReview` **独立评审模块**，没有在 `backend.app`、`backend.p3_relay_app` 或 Render 入口注册；它**只返回无权限元数据**，不重写 ASGI scope、不签发浏览器会话、不证明真实用户身份，不用于记忆保存许可。

## 关键上线阻断

现有 Render `scripts/render_start.py` 启动 Uvicorn 时未明确指定 `--no-proxy-headers`。Uvicorn 可能按其 `forwarded-allow-ips` 信任规则提前改写 `scope.client` / `scope.scheme`，因此 **不能把已经处理过的 request.client 当作可信 TCP 原始对端**。要上线必须先独立审查真实反向代理配置、是否可直接访问后端、运行命令、代理来源网络 ACL 和“入站转发头先清除、再重新写入”的安全规则，并从改写前的传输层取得真实对端（或通过单独受控的 unix socket/sidecar 进行认证）。本模块的 `raw_socket_peer_provider` / `ingress_header_sanitization_proof` 只是注入式离线测试桩，**它们不能从用户头、ASGI scope 或 `request.state` 计算**，也不意味着任何已部署代理确实满足条件。

仅在 `MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED=true` 且独立代理清洗证明严格 `True` 时运行；原始对端必须与事先配置的 1–4 个**精确 IP**相等（拒绝通配符、CIDR 自动扩大、畸形、IPv4-mapped IPv6）。限定单一内部 HTTP ingress Host、内部 TCP 服务地址和审核路径，拒绝已被框架重写为 HTTPS 的 scope；对外固定单个 HTTPS Host。若真实 Render 入口不是这一内部网络拓扑，**模块保持不可用**，不得硬套这些合成配置。

支持一种**严限单跳**的 `X-Forwarded-For` + `X-Forwarded-Proto` + `X-Forwarded-Host` 三元组，或一个 `Forwarded: for=IPv4;proto=https;host=...`（不支持多跳、带引号或 RFC7239 IPv6 形式，拒绝而非推断）。禁止两种格式混用、同名头重复、逗号列表、畸形 IP、内网伪装成外部客户端、TLS 降级、任意用户身份/认证/原始 URL 头和 Host 欺骗。用未合并的原始 ASGI bytes 检查重复头，再由静态允许配置决定边界。

`ReviewedProxyProvenance` 显示 `may_issue_browser_session=False`、`may_write_memory=False`。**源 IP 不是用户身份，代理信任也不是 OIDC 登录或用户同意。** 本阶段只排除一些危险形态的头部伪造；它不保证真实代理严格剥离入站头，不证明互联网客户端真实 IP，也不检查公网证书和 IdP。

继续禁止合并、部署、真实 OAuth/记忆写入/API 调用，v48 正式 **36/36、108/108** 冻结不变。
