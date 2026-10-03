# 新前端接入基线：CORS、P3 与 Codex 0.160.0

核对日期：2026-10-03。此文对应开发分支；合并和部署前不会改变线上进程或环境变量。

## 开发 CORS

所有 relay 入口共用 `backend.cors_config.allowed_origins()`，包括两个 P3 wrapper。HTTP、SSE、上传均走同一个 CORS middleware。预检允许 `Authorization`、`Content-Type` 和所需方法；业务请求仍验证 relay secret。

| 配置 | 行为 |
| --- | --- |
| `RELAY_ALLOW_ORIGINS` | 保留部署者配置的正式来源，逗号分隔；未配置时沿用 localhost/127.0.0.1:8080 |
| `RELAY_CORS_DEV_ENABLED=false` | 默认不附加开发来源 |
| `RELAY_CORS_DEV_ENABLED=true` | 附加开发来源；不改变已有正式来源 |
| `RELAY_DEV_ALLOW_ORIGINS` | 可显式替换开发来源列表，例如一个精确的 Vercel 预览 origin |

开启开发开关、未指定开发列表时，允许 `http://localhost` 和 `http://127.0.0.1` 的 **3000、4173、5173、8080** 端口。不会允许任意 localhost 端口或任意 `*.vercel.app`。

本项目已有正式来源可配置为：

```dotenv
RELAY_ALLOW_ORIGINS=https://ouo-home-ui.vercel.app,https://www.ouoalways.com,https://ouoalways.com
RELAY_CORS_DEV_ENABLED=true
```

只用于需要前端联调的后端环境。使用具体预览地址时设置 `RELAY_DEV_ALLOW_ORIGINS=http://localhost:5173,https://your-exact-preview.example`。值必须是 origin，无路径、查询、凭据或通配符；不合法时启动失败并返回固定配置错误。没有预先虚构或开放尚未提供的预览域名。

## P3 入口差异（与 CLI 版本分开核对）

P3 是归汀自己的 provider/session 契约，`contract_version` 仍为 **1**；它不是 Codex CLI 的版本号。

| 启动方式 | relay | 内部 loop | 主要差异 |
| --- | --- | --- | --- |
| `scripts/render_start.py` | `backend.p3_relay_app:app` | `examples.api_loop:app` | API 路径；不会因单独打开 Codex flag 自动变为 Codex generation 入口 |
| `scripts/render_start_p3.py`，entrypoints 关闭 | `backend.p3_relay_app:app` | `examples.api_loop_provider_guard:app` | API + 持久化 provider 保护；防止既有 Codex 会话误落到 API |
| `scripts/render_start_p3.py`，entrypoints 开启 | `backend.p3_codex_relay_app:app` | `examples.api_loop_codex_canary:app` | 增加 Codex queued ACK、完成回调；公开 relay 不暴露 canary 管理/恢复接口 |

可用 Codex 聊天还要求 `CODEX_CONTROL_ENABLED=true`、`CODEX_CANARY_ENTRYPOINTS_ENABLED=true`、`CODEX_GENERATION_ENABLED=true` 同时成立。新前端根据认证后的 `/app/provider/capabilities` 显示能力，不根据前端版本、会话名称或 CLI 版本自行猜测。

| 契约 | 新前端要求 |
| --- | --- |
| `/provider/status` | CLI 账号/登录状态 |
| `/app/provider/status` | P3 的 provider 与运行状态；不要当作登录状态 |
| `/app/sessions` | 以返回的 `provider`、删除/退役能力为权威；providerless 历史行兼容 API |
| `/app/sessions/{id}/model` | 当前 Codex 会话选型，生成/排队/不确定任务期间拒绝修改；下条消息生效 |
| `/app/send` | 保持 `session_id` 等当前请求字段；Codex queued ACK 表示入队，不能显示为最终回复 |
| `/app/history` | 保留业务消息对象与 session 过滤、since 游标语义 |
| `/app/stream` | 当前 EventSource SSE，使用 token 查询参数；正文/摘要为 `reply_delta`、`thinking_delta`，最终消息仍按现有消息格式处理 |

Codex 当前提供文字会话和完成回传；完整 Codex 增量流式桥接仍是后续工作。这次不把 SSE 改成 OpenAI Responses 或聊天补全流协议。

## Codex 0.147 → 0.160 协议核对

2026-10-03 检查的最新稳定发行版是 [0.160.0，2026-10-01 发布](https://github.com/openai/codex/releases/tag/rust-v0.160.0)，依赖固定为 `openai-codex==0.160.0`。官方[更新记录](https://learn.chatgpt.com/docs/changelog)与安装包二进制 `codex-cli 0.160.0` 一致。

协议依据：实际安装的 CLI 执行 `app-server generate-json-schema --experimental`。归汀使用 `historyMode`、`initialTurnsPage` 等实验字段，因此仅生成普通 schema 不足以核对。

| 核对点 | 结论/适配 |
| --- | --- |
| initialize、设备码登录、model/list | 保持现有协议；实验能力握手继续开启 |
| thread/start、thread/resume 的 effort | 两者均通过 `config.model_reasoning_effort` 传递；移除恢复请求里未声明的顶层 `effort` |
| turn/start 的 effort | 继续使用顶层 `effort`，每轮带上会话保存的模型 |
| ReasoningEffort | 新版为模型目录广告的非空字符串；接入层允许有界安全值，并验证属于所选模型，避免把旧枚举写死 |
| Config.additional | Rust/SDK 成员在原始 JSON-RPC 中展开；从 `config.mcp_servers` 读取，同时兼容旧嵌套夹具 |
| 技能与工具隔离 | 对照该版本官方 `temporary_structured_request.rs`，使用 `cloud.skills.enabled=false`，关闭 context_management，并指定 `default_permissions=:read-only` |
| 权限返回 | start/resume 返回必须确认 `sandbox.type=readOnly` 和 `approvalPolicy=never`；配置意外扩大时拒绝继续生成 |
| 最终正文 | 统一解析 `phase=final_answer`，兼容历史夹具 `finalAnswer` 与无 phase 数据；移除运行链路对 0147 特定补丁的依赖 |

本次 Linux x86_64 安装包内二进制 SHA-256：`12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`。此值记录核对对象，不适用于其他平台发行包。

## 可重复验证

```bash
python -m pip install -r backend/requirements-test.txt
python -m unittest backend.tests.test_codex_0160_contract backend.tests.test_cors_config backend.tests.test_p3_frontend_boundary
```

验证包括：真实 CLI 的临时空账号握手与 config/read、真实二进制生成 schema 后校验发出的线程/轮次请求、两个 P3 入口的认证和 CORS、开发/预览来源隔离。该过程不发起真实账号登录或付费生成。

发布仍先后端再前端；真实设备码授权与登录后对话需要在部署环境验收。回滚 CLI 前先排空未完成任务、保留持久化 CODEX_HOME 和数据库，并保持 P3 provider guard，避免跨 provider 误回退。
