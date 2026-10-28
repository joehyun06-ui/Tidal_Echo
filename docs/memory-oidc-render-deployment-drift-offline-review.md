# 归汀 · Render 部署证据差异审计（默认 OFF，仅离线）

本阶段堆叠 #158 的 Draft 安全分支，**不访问真实 Render 服务，不授权生产部署**。仓库还存在并行的 #159（用量结转预览），不属于此安全链。

## 已在仓库中确认的事实

- `render.yaml` 中 `ouou-home-telegram` 的分支为 `feat/render-telegram-deployment`，而本次核对基线为 #158 Draft 的 `test/memory-oidc-render-ingress-release-interlock-offline-20261028`，HEAD `34a8872ca77a1f11d716514a6c1ff9e6ef0a0e2b`。Blueprint 所声明的分支与未经合并的审核分支**不同**；并不说明线上真的部署了何提交。
- Blueprint 的 `startCommand` 为 `python scripts/render_start.py`，`autoDeploy: false`。根据 Render 官方文档，`autoDeployTrigger` 会覆盖旧版 `autoDeploy`；即使 Blueprint 显示禁止自动部署，也不等于线上服务实时设置得到验证。
- #157 增加的 Uvicorn `--no-proxy-headers` 和 #158 的 Render 启动默认拒绝闸门目前只存在于未合并的堆叠 PR 中；没有确认线上版本采用这些改动。

## 新增能力

`backend.memory_oidc_render_drift_review.assess_render_deployment_drift` 和 `scripts/memory_oidc_render_drift_check_offline.py`：

- 只对仓库 Blueprint 及**可选的、脱敏的、手动提供的 JSON 元数据快照**做字段比较，不调用 Render API、不访问网络、不读密钥，不解析真实用户身份。
- 审核标志 `MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED` 默认 OFF；只可在单独离线审计进程临时启用，Render supervisor 的 #158 闸门会拒绝在服务启动中开启该标志。
- 只接受固定 8 字段的记录：`service_name`、`service_type`、`repository_full_name`、`branch`、`deployed_commit_sha`、`start_command`、`auto_deploy_trigger`、`deploy_status`。额外字段（尤其环境变量、凭证、代理证明、日志）一律拒绝。不允许嵌入真实密钥、cookie 或个人信息。
- 默认没有实时快照，报告明确 `live_service_snapshot_missing`；即便人工填充了**完全匹配**的快照，它也无法自证来源，仍标记 `snapshot_source_not_authenticated`。
- 固定标记尚未验证的真实工作区、反向代理清洗、原始 socket 对端、直接访问网络 ACL、TLS/Host/Origin、真实 IdP、真人同意与记忆写入门槛。
- 报告永远 `may_deploy=false`、`may_enable_browser_login=false`、`may_write_memory=false`。CLI **始终返回非零状态码 2**，绝不作为上线通过信号，异常也只打印无敏感内容的固定类别。

## 离线复现（不连接 Render）

Linux/macOS 本地终端：
```bash
MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED=true python scripts/memory_oidc_render_drift_check_offline.py \
  --review-branch test/memory-oidc-render-ingress-release-interlock-offline-20261028 \
  --review-sha 34a8872ca77a1f11d716514a6c1ff9e6ef0a0e2b
```

这条命令**预期退出码为 2**（阻断），不是失败的安全测试。若后续经过确认的 Render 工作区允许只读查询，先单独核对真实 service/deploy，再由操作人生成仅含上述 8 项元数据的脱敏快照。其来源和真实性必须在审计流程中独立保存证据；把字段填满不等于安全证明。

新增 Windows/Linux `backend.tests._memory_oidc_render_drift_worker`，针对仓库真实 Blueprint 和合成快照测试默认 OFF、已知分支差异、字段精确白名单、坏 SHA、自动部署模式优先级、缺失快照与敏感内容不输出。未修改 `render.yaml`、实际 Render workspace、生产进程或用户数据。

## 生产阻断不变

需要使用者先确认 Render 工作区，才可在后续阶段通过 Render Connector **只读**核对服务的部署分支、实际 live commit、start command、autoDeploy 状态及部署历史。即使获得这些元数据，仍然需要独立验证真实网络代理来源、边缘头部覆盖、内部直连、防伪造 Host/TLS 和安全会话边界；无法从 GitHub Actions 或静态 Blueprint 推断生产可信。

维持 Draft、未合并、未部署；v48 正式冻结 36/36、108/108 不变，真实 OIDC 登录、生产记忆写入及付费 API 不启用。

参考文档：
- https://render.com/docs/blueprint-spec
- https://render.com/docs/deploys
