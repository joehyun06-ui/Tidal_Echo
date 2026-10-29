# 归汀 Stage 43 — 私有真实 SQLite 备份的隔离应用启动验证

## 目的与输入

本阶段承接 Stage42：用户在自己的 Windows 私有目录中已经保存 **真实生产 SQLite 的应用一致性备份**，传输 SHA256、SQLite integrity / FK / schema / migration 均通过；Render 原生磁盘快照仍然无法异盘克隆。第 43 阶段的重点是：**复制已保存的真实备份到隔离临时目录，验证归汀实际 `backend.app` 初始化与内存 ASGI `/healthz`，而不是只运行 SQL PRAGMA**。

生产应用提交固定为 `58ecb255e5cb32e37ee0d949aaa7d8bff896d971`。真实备份、SSH 私钥、密钥密码、聊天正文、数据库文件均不得上传到 GitHub、CI、ChatGPT、Render 非生产公共 URL 或云端同步文件夹。

## 实施方式

- 本 PR 仅提交安全的**通用测试程序**、**虚构数据库**的自动化回归，以及说明。工作流不使用真实数据和任何生产密钥。
- 在 Windows 本机运行 `scripts/stage43_private_restore_probe.py --repo-root <exact-deployed-checkout>`，源码可从本独立 Draft 的工具包取得，但 **RepoRoot 指向原有已核对的 `Tidal_Echo_recovery_verify_58ecb255` worktree**；此程序本身不得要求切换原工作树。
- 原始备份固定读取本机 `%LOCALAPPDATA%/GuitingPrivateRecovery/relay-backup.sqlite3`，不接受任意生产路径和 symlink。应用启动前后重新计算原始 SHA-256；必要时可使用 `--expected-sha256`。
- 把备份副本放到由 `TemporaryDirectory` 创建的用户私有临时目录（绝不把原始备份直接交给 `backend.app.init_db`）。数据写入恢复副本允许；原始备份禁止修改。
- 启动真实项目的 `backend.app` 的 `init_db`，调用 `_database_ready`，进入无 Telegram Worker 的 ASGI 生命周期；使用 HTTPX `ASGITransport` 对内存应用调用 `GET /healthz`。不绑定公网端口。
- 使用已有 `backend.tests._support.load_app` 的合成虚假凭据配置，明确禁用 Telegram、模型供应商生成、KELIVO、记忆写入和 Render MVP 运行模式。运行测试时清空继承的环境变量，并拦截 Python 层 socket 网络连接及 subprocess 调用。
- 恢复副本再次通过 `PRAGMA integrity_check`、`foreign_key_check` 和 `validate_core_schema_v1_v6(require_relay_tables=True)`。原始 SHA 一致之后才打印脱敏 PASS。
- 无论测试 PASS / FAIL，临时目录都会清理。异常只输出泛化类别，不打印路径、数据库内容、栈信息、HTTP 响应内容或用户 ID。

## 可重复的合成 CI

Linux 和 Windows runner 使用虚构 SQLite，通过当前仓库 `channel_store.RELAY_TABLE_DDL` 与真实迁移函数构造测试数据库，执行**同一 Stage43 Probe**，验证启动、健康检查与原始文件 SHA 不变；另有 SHA 错误拒绝与 symlink 拒绝的负面测试。无真实数据库上传。

## 不能被误认为已验证的内容

- `/healthz` + `_database_ready` + ASGI 生命周期成功只证明**隔离禁用功能的应用初始化与核心数据库读取**，并不证明生产完整拓扑（API Loop、Telegram Worker、网络服务）恢复可用。
- 禁用记忆和模型时，不检验真实记忆写入、Telegram 端到端回复、支付 API、附件或 /var/data 下其他资产的恢复。
- Python 层网络拦截是脚本安全保护，不能替代操作系统沙盒的绝对网络隔离。测试不得附带真实服务凭据；建议在独立 Windows 账户或已封闭出站网络环境执行。
- 本次读取**现有私有本机备份**；不重新操作 Render 生产服务，不能据此认证任何历史 Render 原生磁盘快照可恢复。
- 任何启动失败都保留 FAIL / 未验证，不改动线上数据库、不重置线上工作树、也不重新运行生产备份脚本。

## 验收边界

GitHub CI 完成前只称“Draft 已准备”，不得宣称 Stage43 PASS。只有 Windows 上业主报告 `GUITING_STAGE43_RESULT=PASS`，才能把真实备份的隔离应用启动认定为**operator-observed PASS**。保留 v48 36/36、108/108 正式冻结门槛，不执行合并和部署。
