# GM-01A — G3 只读适配与 ProjectionCard 契约

**状态：仅开发候选；不连接生产 G3；不新增线上路由或模型请求。**

基线：归汀 Render 后端当前 Live 提交
`c19f5fa0d4ec2ff3a92f503207698ddd892f6ca4`，目标分支
`feat/render-telegram-deployment`；本功能在独立 Draft PR 中审查。

## 1. 权威边界

- G3 的 source、event、revision、ACL、revocation 是唯一权威。
- 派生数据只称 `ProjectionCard` / `projection_card`，其
  `authority="derived"`。不得把它当成事实签名或重新写入 G3。
- 模块不安装 HTTP 路由、数据库表、启动钩子或密钥；不导入网络、
  OpenAI、AWS、外部 embedding 或运行时服务。
- 原有 G3-QU 冻结账本、7/7 API 历史、正式 36/36、108/108 与生产 HOLD 不变。

## 2. 只读来源契约（拟定接口，不是现有 G3 HTTP 路由）

`G3ReadOnlySourceAdapter` 包含：
- `list_events(after_cursor, access)`
- `read_event(event_id, revision, access)`，`revision=None` 必须读当前权威 HEAD
- `open_source(ref, access)`，必须对精确来源及修订应用 ACL
- `read_revocations(after_cursor, access)`

`AccessScope` 只允许由可信服务端身份构造；不能信任浏览器传入的
`principal_id` 和 `allowed_scopes`。正式适配器还要提供一致性
快照、变更分页、ACL 变动通知及撤销水位，GM-01A 不声称已实现。

## 3. ProjectionCard v1

- `projection_card_id`：由 G3 event、revision、source_id、SHA-256、
  scope、cursor 的规范数组确定性生成。
- `source_refs`：必须保留 `event_id`、`revision`、`source_id`、
  `source_sha256` 与 `scope`。
- `schema_version="g3.projection_card.v1"`，
  `projection_version=1`，`authority="derived"`。
- `layer`：fixed、short_term、long_term。fixed 只接受来源事件上
  已经完成审核的 `reviewed=True`。
- 首版 `projection_text` 只是完整 SHA-256 验证后的 UTF-8 原文字面前
  240 字，不生成总结；`origin="g3_verified_excerpt"`。
- confidence、valence、arousal、importance、retrieval_weight 为空，
  不用模型生成心理/事实判断。
- `source_committed_at` 是 G3 来源元数据，**不是可信第三方时间**；
  同一时间戳也不能证明独立 C 最低历史下界。

## 4. 安全读边界与失效

- `build_projection_card` 读取当前权威 HEAD，核对 G3 原始字节
  SHA-256、版本及 ACL，再次读取 HEAD 检查派生期间有无修订。
- `verify_projection_card` 是投影提供给消费者前的必经路径。
  它从 G3 重建完整派生卡并逐字段比较，不接受未验证缓存。
- 改动一个原文字节、修订升级、撤销、访问权限撤回、伪造权威级别、
  冒充 source_ref 或篡改投影文本均拒绝，不回显原始私密内容。
- `list_projection_delta` 返回 `changed_event_ids` 和新候选卡；
  消费者必须先按事件 ID 清除旧投影和索引，再插入新卡。
  该候选接口**假设 feed 完整且有序**；正式分页与读取快照待实现。
- “遗忘/衰减”将来只能修改召回权重，不能通过投影层删除 G3 来源。
  经授权的删除/撤销是独立的安全流程。

## 5. 本地测试及验收范围

在仓库根目录执行：

    python -m unittest backend.tests.test_g3_projection -v
    python -m compileall backend/g3_projection.py backend/g3_projection_contract.py

本轮 40 条离线合成测试覆盖版本、SHA-256、撤销、ACL、固定锚点审核、
来源并发变化、未授权跨作用域、候选变更水位及污染投影拒绝。

**开发测试 PASS ≠ 生产 G3 接入 PASS。** 不申请生产运行配置。
后续 GM-00B 首先核实真实运行模式、SQLite 路径、磁盘/备份与启用状态；
经额外授权的 GM-01B 才考虑接入真实 G3 只读凭据和生产 ACL。
