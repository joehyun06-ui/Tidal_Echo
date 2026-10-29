# 归汀 Stage 44 — 旧生产真实备份对当前已部署代码的隔离兼容验收

## 执行边界

- **旧版本/备份来源时期**：`58ecb255e5cb32e37ee0d949aaa7d8bff896d971`，Stage42 的生产 SQLite 在线一致性备份存储在 Windows 私有用户文件夹中，Stage43 旧版后端复制恢复启动已通过（operator-observed）。
- **新生产部署版本**：`49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`；Render LIVE、auto deploy OFF。这次部署不是 Stage43/44 审计触发。
- Github compare 旧→新领先两个 Git 提交，触及 `backend/usage_ledger_creation.py`、`backend/tests/test_usage_ledger_creation.py`、`backend/web_context_actions_routes.py`、`docs/continuation-creation-recovery.md`；没有 `backend/channel_store.py` 变更。**文件未变不等同于真实恢复已经通过**。
- 不操作生产服务器、不重新下载/上传真实 SQLite、不触碰 Render 快照、不发送 Telegram、不执行付费 API、OIDC 登录、生产数据库迁移或记忆写入。

## 仅在 Windows 私有本机运行

`scripts/stage44_private_new_release_compatibility.py` 独立于 Stage43：固定新代码 SHA `49ebe9a0...`，拒绝不匹配或已修改的校验工作树，且仅允许读取 `%LOCALAPPDATA%/GuitingPrivateRecovery/relay-backup.sqlite3` 这个已批准的私有备份位置；禁止符号链接、任意数据库路径和上传。

每次运行都：计算原备份 SHA → 建立私有 `TemporaryDirectory` → 复制备份到临时 `test-relay.sqlite3` → 核对临时副本的 SHA → 记录临时库中的消息行数（**仅在内存中比较，不打印**）→ 使用新版本的 `backend.tests._support.load_app` 在此同一个副本执行真实 `backend.app.init_db()` → **强制断言 `app.DB_PATH` 与恢复副本为同一个文件** → 核实恢复前后消息数不变 → sqlite integrity/foreign key/core schema 验证 → 进入 Telegram Worker 禁用的 FastAPI lifespan，用 `httpx.ASGITransport` 完成内存 `GET /healthz` → 重复 SQLite 校验 → 销毁临时副本 → 比对原备份 SHA 不变。

- 将运行时继承的环境变量清除为必要的 Windows 系统白名单（不能读取模型或 Bot 密钥），使用虚假测试配置禁用 Telegram/KELIVO/记忆能力。
- 阻止 Python 层网络连接和外部子进程，唯一允许 asyncio 的内部 loopback socketpair；此机制**不是 OS 层防火墙的形式化证明**，实际用户运行前仍建议关闭互联网连接。
- 不打印用户消息、行数、数据库路径、哈希明文或异常堆栈。输出仅 `GUITING_STAGE44_...` 脱敏 PASS/FAIL。
- 异常产生 FAIL，不能自动解释成数据库不兼容；应分别排查工具环境和 schema/初始化。

## 仅合成数据的 GitHub CI

工作流 `.github/workflows/render-stage44-old-db-new-code-offline.yml` 在 Windows/Linux 两个独立 runner 上运行与实际本机相同的启动程序（但只使用**合成数据库**，不读取任何聊天数据库），检查：已迁移 DB 的旧消息记录在 app 初始化后保留、`app.DB_PATH` 等于临时恢复副本、原备份 SHA 未变、错误 SHA 被拒绝、symlink 来源被拒绝，并运行标准安全与 Python 回归。这个 CI 通过不能替代业主本机使用真实旧备份的结果。

## 判断标准

仅在以下情况可记 **Stage44 真实新旧版本恢复兼容 PASS**：

1. 最终 Stage44 PR 精确源码提交的 Linux、Windows 专项合成 CI 均 SUCCESS，且没有隐瞒其他失败。
2. Windows 操作员用 `49ebe9a0` 精确的独立 worktree、现有私有备份本机运行 `GUITING_STAGE44_RESULT=PASS`，同时证明 `RESTORED_DATABASE_BOUND_TO_APP=true`、`EXISTING_MESSAGES_PRESERVED=true`、`SOURCE_SHA256_UNCHANGED=true`。
3. 报告中仍保留 `PRODUCTION_FULL_RESTORE=NOT_TESTED`；本阶段不覆盖附件、多资产、模型/Telegram端到端、历史原生 Render 快照和 RPO/RTO。

**正式 v48 36/36、108/108 不变。** 全部旧 Draft 仍无需合并，不产生生产部署许可。
