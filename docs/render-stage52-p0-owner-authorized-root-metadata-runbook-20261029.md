# 归汀 Stage52 P0 — 经用户授权的匿名根目录所有者诊断运行方案

> 状态：P0 源码开发和以后受限生产元数据核查获授权；生产尚未执行。2026-10-29。

## 继承门禁

- Stage51 G1B-1 owner-returned STOP_READ_NOT_SAFE；未知是否读取过部分 header，也未知哪一次系统调用出错。禁止重跑原脚本。
- Stage50 G1A 返回 11 个扩展名候选 + 4 个目录，仅为此前元数据结果。P0 不重新检查任何候选，因此输出 candidate_count=UNKNOWN_NOT_ENUMERATED。
- Stage52 PR #190 设计中 P0 与 P1 独立。当前实现**只包含 P0**，P1 将在 P0 结果出来后单独评审；成功 P0 不构成 P1/G1B1 再执行许可。

## P0 的生产边界

- 生产仅允许 Linux 固定根 /var/data；必须同时带 --perform-stage52-p0 和 --owner-authorized-stage52-p0。
- 仅对根目录本身 Path.lstat 两次（不跟随软链接，不打开或枚举目录），检查类型与元数据快照稳定；在进程内比较 st_uid 与 os.geteuid。
- 禁止 os.open、os.scandir、os.listdir、pread、数据库引擎、文件正文或头部读取、权限变更、网络调用、备份、恢复和传输。
- 成功仅输出 root_owner_relation MATCH/MISMATCH；根 UID 不等不代表 O_NOATIME 必然失败，UID 相等也不保证文件安全可读或文件系统支持 O_NOATIME。
- 源码出错只返回匿名 STOP_SCOPE_OR_ACCESS_UNVERIFIED，不输出真实 UID/GID、路径、文件名、文件大小、权限位、inode 或错误原因。
- Windows/Linux CI 只允许临时虚构目录的合成测试，合成入口须有 GUITING_STAGE52_SYNTHETIC_ONLY=true，不能模拟成真正的 Render 审计证据。

## 必须满足的执行门禁

1. 精确最终提交 SHA 上 Windows/Linux 专项 CI 全绿，审查源文件和 git SHA；不能运行未经审定的分支 HEAD。
2. Render 自动部署继续 OFF，LIVE 提交仍是已批准的冻结基线，持久磁盘挂载路径仍为 /var/data。
3. 所有者在自己的 Windows PowerShell 创建独立工作树，确认 SHA，使用已有严格校验 host key 的 SSH，通过 stdin 运行单文件脚本并携带两个 P0 开关。
4. **仅回传最后的匿名 stage=52 P0 JSON**；如 STOP 立即停止，不重试、不提权、不修改模式、所有者或 atime。

## 输出语义

- 成功：stage=52、gate=G1B1_PERMISSION_P0、result=ROOT_OWNER_RELATION_HINT_ONLY，root_owner_relation=MATCH|MISMATCH，candidate_count=UNKNOWN_NOT_ENUMERATED。
- candidate_file_opens=0、candidate_content_bytes_read=0、root_directory_opened=false、root_directory_enumerated=false、filesystem_noatime_verified=false、g1b1_retried=false、source_writes=false。
- 不确认数据库有效性和身份，不解除 G1B-1 安全停止；G1B-2/G1C/G1D 与 v48 36/36、108/108 验收门槛继续冻结。
