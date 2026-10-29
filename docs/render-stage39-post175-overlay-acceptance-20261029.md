# 归汀 Stage 39 — #175 后新生产基线的隔离 Draft 兼容验收

## 范围与来源

- 生产已确认运行 `58ecb255e5cb32e37ee0d949aaa7d8bff896d971`（PR #175 合并后的 commit），Render 自动部署 OFF；实际 `python scripts/render_start_p3.py`。
- 之前基线 `33f2fb9e09d42a901728355720b8334054c7fab5`。
- 旧 Draft PR #170–#174 的 HEAD 仍从上述旧基线分出，各自向当前生产落后 2 个提交且没有和 #175 的五个文件直接重叠。
- 本 PR **不是**为这五个旧 Draft 签发合并许可，不重写、rebase、merge 任何一个原分支，不访问 Render 数据盘、Telegram Token、用户消息或真实模型服务。

## 精确离线叠加实验

新增的 `.github/workflows/render-stage39-post175-overlay-offline.yml` 在每个隔离 GitHub runner 上单独执行：

1. Checkout 仓库完整 Git 历史，校验 SHA `58ecb255`、`33f2fb9e` 和固定的一个旧 Draft HEAD 全部存在，且两条分支共享指定旧基线祖先；若来源不符立即拒绝。
2. 逐文件对比新生产与旧 Draft 在旧基线上的改动，若路径交叉，强制失败且必须人工审阅。
3. 只在 runner 的检出工作树里切换到当前生产 SHA，用 `git apply --check --index` 严格验证原 Draft 从旧基线产生的**精确补丁**，随后在临时工作树应用；不创建远端提交、不更改原分支、不调用 Render。
4. 执行旧 Draft 对应的专项 `unittest`、新生产代码的完整 `backend/tests/test_*.py` 回归、源码编译，并再确认没有打开健康/证据获取开关且没有 Telegram/Render/OpenAI/Kelivo 密钥。
5. 五个矩阵成员全部必须通过才可认为**候选组合在 CI 隔离环境下兼容**。这仍不是现实生产合并验收，更非业务端到端验收或可恢复备份证明。

| Draft | 固定 HEAD | 专项 |
|---|---|---|
| #170 | `e1df2077` | Blueprint、OIDC ingress/drift |
| #171 | `b8247b34` | Render deployment evidence audit |
| #172 | `fdd98e9a` | Public health/readyz fixed HTTPS OFF |
| #173 | `afba43f7` | Telegram webhook and disk metadata OFF |
| #174 | `b59a8154` | Operator-local explicitly enabled fixed GET OFF |

CI 不包含现场 Bot API 请求，也不做真实公网 GET、SQLite 写入或磁盘恢复。测试可使用完全合成的临时数据库和 loopback 服务器。

## 已证实的真实运行情况

用户在部署 #175 后通过本机 HTTPS 观察：
- `/healthz` HTTP 200，`ok=true`
- `/readyz` HTTP 200，`ready=true`，`status=ready`，13 个已报告检查项
- Telegram `getWebhookInfo` 匹配预期登记目标，pending 0，无已报告历史错误；**仍未测真正消息投递/生成**
- Render 控制台快照清单 7 条，2026-10-22 至 2026-10-28；**仍未测恢复、SQL 一致性或旧 schema 兼容性**

Render 的历史两次聚合 502 靠近部署期，缺少请求级日志；保留未知根因，不执行回滚。

## 无授权的生产活动继续禁止

保持 v48 **36/36、108/108** 正式验收门槛冻结。真实 OIDC 开启、记忆写入、付费 API、Telegram 发消息/人工端到端试验、部署/回滚、磁盘 Restore 需要独立明确许可，不能从 CI 成功推断可以执行。当前所有 Draft 继续独立且未合并。

## 待验收

本 PR 创建时仅提交测试矩阵和此说明，**不能将 workflow 创建等同于测试成功**。必须等待本 PR 确切 HEAD 的 5 个叠加任务及同一个 HEAD 的全部安全工作流通过，逐项读取 GitHub Actions 原始 job 日志，记录任何失败/跳过后再更新验收结论。
