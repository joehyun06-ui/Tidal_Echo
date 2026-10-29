# 归汀 Stage 40 — 新生产基线五 Draft 同时叠加的离线验收

## 为什么还要这一轮

Stage 39 #176 在 Render 当前部署 commit `58ecb255e5cb32e37ee0d949aaa7d8bff896d971` 上，**分别**叠加 #170–#174，已得到 32/32 workflows、65/65 jobs，五个叠加工作树分别完成 2553/2553 Python 回归。但这只证明单个 Draft + 当前生产基线各自兼容，未证明五份代码**同时**存在时能无冲突运行。

Stage 40 针对这个更强的、仍完全离线的候选组合做检验。原生产分支和所有旧 Draft 均不修改。

## 冻结输入（不可自动漂移）

- Render 已确认的生产 SHA：`58ecb255e5cb32e37ee0d949aaa7d8bff896d971`
- 旧共同祖先：`33f2fb9e09d42a901728355720b8334054c7fab5`
- #170：`e1df2077aa8ce0b86164eb6aae39484177ce13f0`
- #171：`b8247b345f8f675abe2452c335dc15089ce46be7`
- #172：`fdd98e9a1f824e5c1cbffe1ea18dcb7b77f4f9f3`
- #173：`afba43f7e397e3f5299f3035f17352ea6228b838`
- #174：`b59a81541cd0b9ce5511a3eafc4dc4c965e31061`

两组独立 GitHub Actions runner 按 170→174 和 174→170 的反向顺序验证：

1. 检查所有 SHA/共同祖先真实存在，不准默默换最新 HEAD。
2. 每个旧 Draft 与生产新增文件集比较；任何重复路径直接失败，不自动解冲突或覆盖。
3. 从**部署的确切 commit** 做 detached checkout，按顺序 `git apply --check --index` + `git apply --index` 五个独立补丁；结束后严格验证生成的文件集合恰好等于已批准的五份变更文件并满足 `git diff --check`。
4. 在已经**同时包含五份补丁**的唯一工作树上，运行全部相关专项 `unittest`，再运行 Python 完整 `test_*.py` 回归并编译源码。
5. 检查运行时开关全部关闭、敏感凭据变量为空、基础代码仍指向部署 commit。GitHub Actions 仅授予 `contents:read`，checkout 不保留任何 git 凭据。

以上操作只改临时 CI runner 内的 index 和工作树，不提交合并产物，不调用 Render API、Telegram Bot API、真实付费模型/身份登录，不写生产 SQLite、记忆、配置、磁盘，不触发生产 webhook。

## 能证明与不能证明的内容

成功：**两个指定顺序**中，同一个隔离工作树能无文件冲突地同时包含五份 Draft 的精确补丁；所有专项和 Python 回归检查通过；不代表对所有运行时状态和配置组合做穷举测试。

不证明：实际合并到目标分支后的全部 CI、新版候选 SHA 被发布、Telegram 消息端到端可用、历史 502 的请求级根因、Render 快照恢复后的 SQLite 一致性、代码回滚时的数据库兼容性。不能从 CI 通过推断可执行任何生产变更。

## 验收规则

只能对当前 Stage 40 PR 最终 HEAD 宣布：

- GitHub Actions 工作流逐项 `success`；
- 两组 combined overlay 任务各自完成全部步骤；
- 两个完整 Python suites 以及所有专项有原始 `Ran N tests / OK` 日志；
- 原 PR #170–#174 和 Stage 40 Draft 未合并，Render 实际 live SHA 与自动部署开关复查无变化。

如果任何项失败，只修复这个隔离 Draft 的工作流程，再从新的确切 HEAD 重新验收。不做生产部署或回滚。

保持 v48 **36/36、108/108** 正式验收冻结。真实 OIDC、记忆写入、Telegram 端到端生成、付费 API 均未经本阶段授权。