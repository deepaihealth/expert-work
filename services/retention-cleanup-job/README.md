# expert-work-retention-cleanup-job

Expert Work **retention cleanup job** — deletes expired rows from
`audit_log` / `event_log` / `jwt_blacklist` according to per-tenant
retention configured in `tenant_config`. Stream D.3.

## Scope (D.3)

- `audit_log` — only rows that are **already backed up** (`backup_acked = true`).
  Unacked rows are skipped + counted; persistent skips mean the D.1c
  WORM backup worker is falling behind and need attention.
- `event_log` — by `created_at` only (no WORM in M0).
- `jwt_blacklist` — by `expires_at` only (global, no tenant_id).

Per-tenant retention comes from `tenant_config.audit_retention_days`
(default 90) and `tenant_config.event_log_retention_days` (default 30).

## DB role

Runs as `retention_cleanup_worker` (NOLOGIN BYPASSRLS, created in
migration 0010) — column-narrow DELETE grants on the three target
tables; SELECT on `tenant_config`. The main app role does NOT have
DELETE, which preserves the D.1a append-only contract.

## Entry point

```bash
uv run python -m retention_cleanup_job
```

Default mode runs one sweep and exits — meant to be wired into cron
or a Kubernetes CronJob. Settings via `EXPERT_WORK_RETENTION_*` env (see
`settings.py`).

## Workspace rules (留存链 PR2,波 3 线 R)

用户 2026-09-09 拍板:**产物 90 天、上传 90 天、已删工作区库行 90 天销账**。
三条规则碰 NAS 工作区文件,需要 `EXPERT_WORK_RETENTION_WORKSPACE_ROOT`
(CronJob 挂与 control-plane 同一个 PVC);不配则整体跳过并打 warning
(只删行不删文件会留下永久孤儿,所以没有「半开」)。

B-63 起**上传按最后使用到期**:`greatest(created_at, last_used_at, 会话最后一次 run)` 加上
租户的 `tenant_config.upload_retention_days`(为空用 `EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS`,
默认 90)。`last_used_at` 在对外接口重新附加(登记行;跨会话就是源会话那一行)与对外下载时记,
每行每天最多一次,且只在 run 被接受之后记。「过期 7 天还失败就放弃」从 `max(到期, tenant_config.updated_at)` 算(租户刚改小天数时不会第一次失败就放弃),最多推迟 30 天;已过到期 7 天仍在失败的行计 `upload_rows_failing_overdue` 并让作业非 0 退出。租户可设 7–365 天,改小要显式确认。产物 / 上传两个 env 窗口限 7–365,超出的夹回范围并打
`retention.retention_days_clamped` warning(不拒跑);默认 90 必须与协议的 `FILE_RETENTION_DEFAULT_DAYS` 一致。

B-283 起**图片(`image_upload`,对象存储里的字节)也按最后使用到期**:同一个时钟
`greatest(created_at, last_used_at, 会话最后一次 run)`,天数用同一个
`tenant_config.upload_retention_days`(图片也是附件;为空用与附件同一个 `EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS`,
默认 90,同样 7–365;老的 `EXPERT_WORK_RETENTION_IMAGE_RETENTION_DAYS` 不再生效,配着会打 warning)。`last_used_at` 在对外下载图片成功时记(每张每天最多一次);会话里的使用(提问、`ask_image`、
对外 `files[]` 附图都要求是本会话的图)由「会话最后一次 run」覆盖。用户手动删过的图照旧下一轮就清。
图被清掉之后,会话历史里还引用它的轮次不再失败:模型那一侧换成一句「图片已不存在」的文字。

| 规则 | 删什么 | 不删什么 |
|---|---|---|
| B-28 产物版本 | 最后使用满租户保留期(默认 90 天;B-63:一版自己的 = 登记 / `last_used_at`(三个下载端点记)/ 登记它的会话最后一次 run 最晚的那个,最新版与产物行 = 整条产物最晚的那个,所以产物离开列表与最新版被清同时发生;版本 pass 之后还剩版本行的产物不软删):`path_in_workspace` 那一个文件 + 版本行;版本清空的 `artifact` 行标 `deleted_at`(`ARTIFACT_EXPIRED` 审计) | 同名产物的新版本;文件所在目录;删不掉的文件对应的行(明天再试) |
| 上传 | 最后使用(见上)满租户保留期(默认 90 天):按会话**当前**的 agent / 布局算出的 `agents/<key>/[threads/<会话>/work/]uploads/<file>`(B-271)+ 标 `deleted_at` | 图片类 `expert_work://image/…` 的字节(归 `image_upload` 自己的 pass);会话已不在的行的文件(对话视图的目录随会话 purge 走;**老视图的文件在 agent 共用的 `agents/<key>/uploads/` 里,purge 不删、这里也不删,留在那里**);还有没到期的行指着的文件;大小 / sha256 与登记时不符的文件(没有 sha256 的老行先从登记时的 `DOCUMENT_UPLOAD` 审计行补,补不到只比大小);路径不可用的(某段是软链 / 不是目录、叶子不是普通文件,或用户子树里除暂时性以外的任何 I/O 错误:`chmod 000`、套接字……,计入 `upload_files_unsafe`)—— 以上几种行照样标 `deleted_at`;暂时性 I/O 失败的行(明天再试,计入 `upload_rows_failed`;过了到期日 7 天还在失败就只标行、不动文件,计入 `upload_rows_abandoned`);「放弃」(路径不可用、过期太久)先看完全批再标:涉及超过 3 个用户就一条都不标(`upload_rows_held`,像环境问题);有行被放弃或被拦下,pass 记失败;某个用户的 `{tenant}` / `{user}` 目录打不开(不是不在;没绑 agent 的会话以整个用户根为 `/workspace`,模型 `chmod` 得到)时只跳过这个用户这一轮的行、pass 记失败(`upload_owners_unavailable`),超过 3 个用户或工作区根本身打不开时整个 pass 中止、行不动 —— 以上都进 `failed_passes`,进程退出码 3;没有登记行的文件:B-281 之前的控制台上传与跨会话复制,以及 B-281 起落在共享根(老视图的 agent 根 / 没绑 agent 的用户根)的控制台上传与复制(落进对话自己目录的才登记);老对话迁移后留在 agent 根的原件不归这里,见下方 `legacy_originals` |
| B-282 闲置会话的工作目录 | 会话最后活动(`greatest(会话创建, 最后一次 run, 本会话没删的文档上传最后一次被用)`,持锁再查时另看到期线之后的 `DOCUMENT_UPLOAD` 审计)满租户的 `tenant_config.conversation_workspace_retention_days`(为空用 `EXPERT_WORK_RETENTION_CONVERSATION_WORKSPACE_RETENTION_DAYS`,默认 90,7–365):对话视图会话的整棵 `agents/<key>/threads/<会话>/`(`work/`、`PLAN.md`、`.seeded` —— 会话再跑时重新开场复制),以及这段会话每个 run 在 agent 根(没绑 agent 的在用户根)下的 `.tool_results/<run>/` —— **改名挪进隔离区** `{tenant}/.purged-agents/{user}/`(沙箱挂不到,满 7 天由隔离区那一条删,7 天内可挪回);持开场复制的 advisory lock(classid 8624,键 `{tenant}:{会话}`)只做再查 + 改名 + 记,上传落盘拿同一把锁的共享形式;改名后再查,期间起了 run 就挪回原处、记 `conversation_dirs_raced`、不记;路径逐段 `O_NOFOLLOW`;清完记 `thread_meta.workspace_cleaned_at`(下一轮起 run 时清回空并给模型一句说明),之后有新 run / 上传才重新算;每轮最多 `EXPERT_WORK_RETENTION_CONVERSATION_WORKSPACE_BATCH_SIZE`(默认 500)段 | 库里的行(会话、历史、产物登记);还有没删的文档上传登记行的会话(下载读这段会话的 `work/uploads/`;登记行由上传那一条按租户上传保留期到期后才轮到这里);还有 pending / queued / running / paused 的 run 的会话(删前再查一次);目录里有 B-235 之前登记、仍是最新版(下载读工作区)的产物文件的会话 —— 整段不动(`conversation_dirs_skipped_artifact`);路径中途有符号链接的(`conversation_dirs_skipped_unsafe`);用户已软删的(`tenant_user.deleted_at` 在查询里就排除;NAS 上有 `.deleted/<user>` 的也跳过 —— 归 janitor 归档,不记清理时间);列到了却没清的记 `thread_meta.workspace_clean_attempted_at`,下一轮排在没试过的后面;`.tool_results/<run>` 里有下载要读的产物文件的 run 目录(`conversation_tool_results_skipped_artifact`);老视图会话的 `threads/<会话>/`(只删它的 `.tool_results/<run>/`)。删失败的 `conversation_dirs_failed`,不记清理时间、明天再试,pass 记失败(退出码 3) |
| B-284 agent 根存量 | 切换时刻 `T` 钉在 `EXPERT_WORK_RETENTION_AGENT_ROOT_CUTOVER`(带时区 ISO;测试 `2026-10-30T23:48:00+08:00`、生产 `2026-11-04T19:10:00+08:00`;不设 / 写错 = 不跑,写错还记失败);「早于 `T`」与「满天数」都看 `max(mtime, ctime)`(mtime 用户态能拨,ctime 不能)。agent 根**顶层文件**:早于 `T`、没登记成产物、满租户的 `artifact_retention_days`(默认 90),按判定时的 inode 删;**子目录**:整棵树每一项都早于 `T`、没有产物文件、这个用户在这个 agent 下闲置满同样天数 → 按 inode 改名挪进隔离区 `{tenant}/.purged-agents/{user}/`(沙箱挂不到,满 7 天删,7 天内可挪回)、在那里复查、复查不过挪回原名;顶层文件删之前那一次 `lstat` 再核对 inode 与「还早于 `T`」;`T` 晚于现在 = 写错,不跑、记失败;`EXPERT_WORK_RETENTION_SHARED_DELETE_AFTER`(只认 `YYYY-MM-DD`,不早于 2026-11-06)到了 → 每个用户根下的 `shared/`。一律 `safe_remove`;计数 `agent_root_files_removed` / `agent_root_dirs_removed` / `shared_dirs_removed` / `agent_root_skipped_unknown` / `agent_root_skipped_symlinks` / `agent_root_skipped_changed` / `agent_root_failed`;每轮打 `retention.agent_roots_gate` | 点开头的名字、`AGENT_NOTES.md` / `MEMORY.md` / `PLAN.md` / `TODO.md`、平台目录、符号链接、`T` 之后改过的任何东西(看 ctime,`touch -d` / `unzip` 保留的老 mtime 骗不过)、登记成产物的文件、判不了的大子树;没设 / 写错日期时的 `shared/` |
| B-284 `/agent` 文件版本 | `{tenant}/.agent-file-versions/{user}/{key}/{hash}/`:按 `path` 逐段 `lstat` 确认源文件不见、`missing` 标记满 30 天 → 整个版本目录;每个文件最新 3 份之外、写下(ctime)满 90 天的版本;满 1 小时的 `.tmp-*`。计数 `agent_file_version_dirs_dropped` / `agent_file_versions_capped` / `agent_file_versions_failed` | 源文件在的;路径中途有符号链接 / 叶子不是普通文件(「不知道」,不动标记);已软删用户 |
| B-27 threads/ | `threads/<thread_id>/` 且 `thread_meta` 行不存在 | 行还在的目录(**不按时间删活会话**);非 UUID 名的子目录;已软删用户的整棵树 |
| X-4 ② 已删工作区 | 软删且已归档满 90 天的 `user_workspace` 行 + 该用户的 artifact / artifact_version / user_upload 行(`WORKSPACE_HARD_DELETE` 审计) | OSS 归档对象(桶生命周期到期);`.deleted/<user>` 标记 |

**不变式**(`tests/test_workspace_invariant.py`):对活着的工作区,job 只删
(a) 登记过的产物文件 (b) `uploads/` 下登记过的文件 (c) 孤儿 `threads/<id>/`
(d) B-282 闲置满保留期的会话自己的 `threads/<会话>/` 与它的 run 的 `.tool_results/<run>/`;
agent 根任何其它文件/目录 —— `style/`、`MEMORY.md`、未登记文件 —— 永不触碰。
碰文件的代码只有 `workspace_files.py` 的两个函数,没有任何「按时间扫目录删文件」。

**B-281 —— 迁移后留在 agent 根的上传原件**(`legacy_originals.py`,不在每日作业里):老对话迁移
(B-262 P3)把上传复制进对话目录,agent 根下的原件没有登记行再指着它。两个入口跑同一套谓词:
control-plane 每个对话迁移成功后在后台对那个 (租户, 用户, agent) 跑一次(只要还有老视图会话就
不删,所以实际是最后一个老视图会话迁完时清);存量用一次性命令
`python -m retention_cleanup_job.legacy_originals`(默认只数,`--apply` 才删,只打计数)。只删逐字节
重复的原件:有登记行指着、每条引用(登记行 + `DOCUMENT_UPLOAD` 审计)的副本都在且 sha256 相同、
同 agent 同用户(或无用户)不剩老视图会话、没有无副本的产物版本读它、路径上没有软链;只有审计的
原件不碰。谓词全文见模块头,验收见 runbook §97。

会话 purge(`POST /v1/sessions/{id}:purge`)同步删 `threads/<id>/`;这里的
孤儿扫描只兜钩子失败与存量。
