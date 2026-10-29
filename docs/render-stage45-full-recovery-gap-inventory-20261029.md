# 归汀 Stage 45 — 全量持久化恢复差距盘点（读配置 + 只读文件元数据）

## 生产基线与不可越过的边界

- 2026-10-29 Render live service `ouou-home-telegram`, disk `dsk-d9dk0gbrjlhs73aph4ig`, 挂载 `/var/data` 1 GB，自动部署 OFF，生产源码 `49ebe9a0e7f391efda86585b2bbe8f1bcff4288f`。部署以 Render 查询结果为准，不从 Blueprint 猜测。
- 生产当前真实数据、Telegram 收发、记录、附件、密钥、磁盘不可修改。现有真实 `relay.db` 备份在业主本机私有目录，曾经过 Stage42 文件校验、Stage43 旧版、Stage44 新版隔离启动；**不等于**全磁盘恢复。
- 本阶段只根据**现有代码和 Render service metadata**生成期望路径，再由业主在已有 Render SSH 会话运行**只读元数据清点**。不能回显真实文件名、内容、私钥、环境变量值，不能调用原生快照 Restore。GitHub CI 只运行合成路径测试。

## 从当前生产仓库确认的期望清单

| 分类 | 来源 | 期望路径或配置来源 | 当前证据 / 缺口 |
|---|---|---|---|
| 核心消息与记录 SQLite | Blueprint RELAY_DB; app.py; channel_store.py | `/var/data/relay.db`，旁边可能出现 `-wal/-shm` 过渡文件 | **真实 DB 在线一致备份、Windows SHA / integrity / FK、Stage43 旧后端、Stage44 当前后端隔离启动通过**；本阶段不再复制 |
| 附件/图像/音频 | Blueprint RELAY_UPLOAD_DIR + app.save_upload_bytes | `/var/data/uploads/` | 真实目录是否存在、文件数和字节数、是否有子目录/链接、业务引用完整性未验证；仅 SQLite 备份不包含附件内容 |
| 脑目标状态 | Blueprint RELAY_BRAIN_FILE + deployment_config.initialize_brain_target | `/var/data/brain_target` | 代码允许以 `loop\n` 重建，但运行时实际状态、重新创建前是否有额外状态未检验；文件非 SQLite |
| API Loop 配置 | Blueprint LOOP_CONFIG + deployment_config.validate_loop_config_file | `/var/data/api_loop.config.json` | **重要启动依赖**，真实配置未备份/恢复；不能无依据认为可从仓库样例完全重建，配置可能含敏感参数，禁止显示正文 |
| Codex home/workspace | Blueprint CODEX_HOME/CODEX_WORKSPACE + codex_control | `/var/data/codex-home`、`/var/data/codex-workspace` | 条件运行目录。Blueprint `CODEX_CONTROL_ENABLED=false`，但不能从 Blueprint 推定线上所有运行时 flag 和实际文件内容。需要仅元数据检查 |
| Codex generation store | `backend/codex_generation_runtime_config.py` | 默认 `/var/data/codex-generation.db`（仅有关功能启用时） | **可能是与 relay.db 分开的独立 SQLite**，是否存在、含何数据、是否需要独立一致备份都未核实 |
| Usage ledger DB + plan | `backend/usage_ledger_entrypoint.py` + preflight | 启用后，由 `GUITING_LEDGER_PLAN` 指向 persistent root 内 JSON，`storage.ledger_db` 是另一条独立 DB 路径 | **不可从固定文件名猜实际位置**。本轮绝不读取或打印 plan 内容及密钥。应由受权操作员在本机私有环境核对路径后再决定备份策略 |
| Usage ledger 私有签名密钥 | `backend/usage_ledger_secret_files.py` / entrypoint | `GUITING_LEDGER_CONTINUATION_KEY_FILE`、`GUITING_LEDGER_FINGERPRINT_KEY_FILE` 等，仅在对应模式存在时 | **绝不上传/回显/复制到 GitHub 或聊天**。恢复需要独立密钥托管，核实恢复后签名验证失败风险 |
| 外部部署凭据 / webhook | Render 服务 Secrets 与 Telegram 等 provider | 不应纳入无加密附件包；来自 Render/其他独立服务 | 只核实所需变量名及恢复步骤，不收集任何 secret 值，不能从代码声明直接推断生产变量已配置 |

代码中还有可能出现不在上述固定清单内的文件；只读根目录汇总会用匿名计数标识“未分类项”，**不输出其文件名**。在运营者审核前，不得自动归类并打包。

## 只读元数据盘点程序

`scripts/render_stage45_persistent_inventory_readonly.py`：

- 默认固定检查 `/var/data`，必须显式 `--perform-readonly`，无 SSH、网络、数据库连接、文件创建、删除或写入代码。
- `lstat` 不跟随符号链接；固定类别只返回 `present/missing/type_mismatch/inaccessible`、文件字节数，已批准目录的文件/目录/符号链接合计数量及总字节数；最多遍历 10,000 个条目、3 层深度。未分类根目录条目只报告合计，拒绝泄露名字。
- 绝不打开文件读内容，不枚举环境变量，不访问 `GUITING_LEDGER_PLAN` 或密钥文件。输出即便是脱敏 JSON，也不等于真正检验可用性、权限恢复、SQLite 内部一致性或附件内容。
- `--synthetic-root` 只能在 `GUITING_STAGE45_SYNTHETIC_ONLY=true` 下运行，是专供 Linux/Windows CI 测试的另一条路径。
- **真实生产执行需经业主操作**，把此脚本的源代码经已有受信任 SSH stdin 执行，不必上传文件到 Render，不再询问任何 SSH/数据库密钥，也不将运行结果包含文件原名的 Shell 跟踪发给聊天。

## 验收与缺口优先级

1. 本轮 GitHub Linux/Windows 合成元数据测试成功只证明脚本不会读取文件正文或主动泄露文件名；不能代替真实磁盘扫描。
2. 业主只提供脱敏的 `stage=45` JSON 后，才可宣布**实时磁盘已完成元数据清点**，但仍不代表附件、配置和密钥可恢复。
3. 优先级 **P0**：API Loop 配置与 uploads；P1：实际存在的独立 Codex/usage-ledger DB、密钥及所有“未分类项”；P2：可重建 `brain_target` 和禁用功能的空目录。
4. 单个 SQLite 的成功恢复不代表全系统可恢复；历史 Render 原生快照异盘克隆不支持，仍未恢复验证；文件间一致性与 SQLite RPO/RTO 仍未证明。
5. 不创建付费恢复环境、不启动模型/Telegram、不读取敏感文件、不触发真实磁盘 Restore、不合并 Draft、不部署。正式 v48 **36/36 与 108/108** 冻结。

**现实盘点完成之后的下一步**：针对实际存在、已批准的非 SQLite 文件，分别设计加密备份与隔离校验，不直接复制所有目录或一次性打包未分类数据。
