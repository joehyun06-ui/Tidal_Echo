# API 配置、Codex CLI 登录与会话模型

配套前端：`joehyun06-ui/ouo-home-ui` 的 `provider-settings.js`。入口为个人信息页的「连接与模型」。所有公开接口继续使用归汀的 `Authorization: Bearer <RELAY_SECRET>`；此密钥与 API Key、Codex 账号凭据不同。

## 接口

| Method | Path（公开 relay 路径，不含 `/relay` 前缀） | 用途 |
| --- | --- | --- |
| GET | `/app/provider/api/config` | `{url, model, has_key, scope: "all_api_sessions"}`，不返回密钥 |
| POST | `/app/provider/api/config` | `{url, model, key?}`；同一 URL 可省略密钥；改变 URL 必须重新输入密钥 |
| GET | `/app/provider/api/models` | 用服务器保存的配置读取 `/models`，只返回 `{models: [id, ...]}`；不支持列表时允许手填模型 |
| GET | `/provider/status` | Codex 登录状态；`connected` 和 `login_status` 为权威状态 |
| POST | `/provider/login/start` | 返回官方 `verification_url`、一次性 `user_code`；前端轮询状态 |
| POST | `/provider/login/cancel` | 取消当前授权尝试 |
| POST | `/provider/logout` | 退出服务器上的 Codex 账号 |
| GET | `/provider/models` | 从运行中的 CLI 分页读取模型、默认推理强度和支持的强度 |
| GET | `/app/sessions/{id}/model` | 当前 Codex 会话的模型和推理强度 |
| POST | `/app/sessions/{id}/model` | `{model, reasoning_effort?}`；保存后从下一条消息生效 |

API 设置写入现有持久化 `LOOP_CONFIG`，保留会话、历史数量等其他字段。API Key 不回传、不写进浏览器存储；上游模型目录请求不跟随重定向，错误正文不透传。

Codex 模型保存在现有 `codex_sessions` 行，不改变 provider、thread、persona 或历史。选择会检查当前 CLI 目录；存在排队、生成、回调未完成或不确定任务时返回 `409 codex_generation_busy`。首次 thread/start、后续 thread/resume 和 turn/start 都使用会话中的模型/强度。新建会话仍采用部署默认模型，可在面板中再选择。模型目录表示 CLI 提供的选择，不保证账号对每个模型都有推理额度。

## 发布顺序

1. 先发布后端，再发布前端。API 配置和 Codex 登录/模型目录在现有 loop 入口上可用。
2. 登录功能需要已有的 `CODEX_CONTROL_ENABLED=true`，凭据继续由 CLI 存在持久化 `CODEX_HOME`，前端只展示设备码。
3. **若要使用 Codex 会话及模型切换**，Render 的 Start Command 必须为 `python scripts/render_start_p3.py`，并显式启用 `CODEX_CONTROL_ENABLED=true`、`CODEX_CANARY_ENTRYPOINTS_ENABLED=true`、`CODEX_GENERATION_ENABLED=true`。当前默认 Blueprint 的 `render_start.py` 不会启动 Codex generation。此改动不自动修改线上 Start Command 或开关。
4. 保留 `/var/data` 持久化磁盘、现有 API 配置、内部 loop token、relay secret 和渠道设置。Codex 沿用既有文字消息与会话归属限制。
5. 打开个人信息页 → Codex CLI → 登录，用户在官方授权页完成设备码确认；点「使用 Codex」，选择模型和推理强度，再点「应用到当前 Codex 会话」。

## 验证

后端新增 `test_provider_settings.py`，覆盖鉴权、密钥保留/重定向隔离、磁盘持久化、CLI 分页、模型请求参数、未完成任务禁止切换。既有 provider control、worker、P3 session guard/delete 测试保留。

CLI 协议对照项目锁定版本 `openai-codex==0.160.0` 生成的 JSON schema；无需真实账号完成自动化测试。真实设备授权、账号额度与线上请求要在部署后由用户账号验证。
