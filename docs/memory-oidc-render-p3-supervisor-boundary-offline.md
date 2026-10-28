# 归汀 · 真实 Render P3 启动链安全边界（离线，默认 OFF）

本轮已经得到用户确认，在 Render 工作区 `ouott` 对 `ouou-home-telegram` 完成**只读**服务和最近部署核对（没有修改任何 Render 资源）。生产服务实际启动命令为 **`python scripts/render_start_p3.py`**，不是当前仓库 `render.yaml` 的 `python scripts/render_start.py`；自动部署触发器为 `off`。最后一条 `live` 部署是 **2026-10-23，commit `a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c`**；当时的基础 `scripts/render_start.py` 不含 `--no-proxy-headers`，也不含 #158 的 OIDC 启动闸门。这个事实不能通过后续 Draft 上离线 CI 自动修复。

## 实际 P3 入口调用链

`scripts/render_start_p3.py` 调用 `scripts/render_start.py`，在基础子进程命令上按 Codex 资格开关选取：

| 模式 | 内部 `api_loop` | 公网 `relay` |
| --- | --- | --- |
| 默认 P3 | `examples.api_loop_provider_guard:app` | `backend.p3_relay_app:app` |
| 显式 Codex 双开关 | `examples.api_loop_codex_canary:app` | `backend.p3_codex_relay_app:app` |

原来的 #157 安全参数加在基础 supervisor 中；在真实部署入口添加独立检查，避免基础入口或 P3 命令转换意外删除保护却继续启动。

## 本轮代码变更

- 在 `render_start_p3.main()` 最开头重新执行 #158 的 `reject_unreviewed_browser_identity_ingress`；碰到 OIDC 审核或真实网络放行类标志误开时，执行固定类别的安全日志，返回 **2**，不安装命令选择器、也不启动服务。
- 在 `_select_child_commands` 中，在调用基础选择器前再次执行同一个安全闸门，覆盖调用方直接使用 `child_commands` 而绕过 `main()` 的情况。
- 两种 P3 模式的两个子进程都必须**唯一**包含 `--no-proxy-headers`，不得包含 `--proxy-headers` 或 `--forwarded-allow-ips`；`api_loop` 仅监听 `127.0.0.1`，`relay` 继续保持既有 `0.0.0.0`。违反时固定报 `p3_proxy_header_boundary_unreviewed`，在构造子进程命令阶段中止。
- 默认 Codex 仍 OFF；Codex 模式只在既有 `CODEX_CANARY_ENTRYPOINTS_ENABLED=true` 与 `CODEX_GENERATION_ENABLED=true` 组合下接受，并且不放松任何 OIDC 或代理入口边界。
- `render_start_p3.main()` 继续恢复基础模块的命令选择器，即使测试制造基础调用异常也会恢复。没有修改 `render.yaml`、部署命令、生产环境、IdP 或记忆写入开关。

## 离线验收与生产阻断

新增 Windows/Linux 专项 `backend.tests._memory_oidc_render_p3_boundary_worker`，覆盖默认 P3、显式 Codex 模式、双子进程代理头严格禁用、反向恶意配置、绕过基础 preflight、P3 最早安全拦截、无法生成身份授权、选择器还原。全部测试阻断真实 socket 网络和外部服务。

**生产仍不可宣称通过：** 真实 Render live 版本仍是 `a2d8699a`；本阶段仅把对未来 P3 入口的约束提交为 Draft。Render 当前允许来自 `0.0.0.0/0` 的公网入口，真实边缘代理对端、代理头清洗和 TLS/Host/Origin 可信证明仍未完成；禁止使用 #156 模拟回调作为真实认证证明。真实 OAuth/IdP 登录、浏览器会话授权、真人同意、记忆写入和付费 API 不启用。所有安全 PR Draft、未合并、未部署；v48 冻结 **36/36、108/108**。
