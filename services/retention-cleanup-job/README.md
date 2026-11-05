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
每行每天最多一次。产物 / 上传两个 env 窗口上限 365,超过的夹到 365 并打
`retention.retention_days_clamped` warning(不拒跑)。

| 规则 | 删什么 | 不删什么 |
|---|---|---|
| B-28 产物版本 | `artifact_version.created_at` 满 90 天:`path_in_workspace` 那一个文件 + 版本行;版本清空的 `artifact` 行标 `deleted_at`(`ARTIFACT_EXPIRED` 审计) | 同名产物的新版本;文件所在目录;删不掉的文件对应的行(明天再试) |
| 上传 | 最后使用(见上)满租户保留期(默认 90 天):按会话**当前**的 agent / 布局算出的 `agents/<key>/[threads/<会话>/work/]uploads/<file>`(B-271)+ 标 `deleted_at` | 图片类 `expert_work://image/…` 的字节(归 `image_upload` 自己的 pass);会话已不在的行的文件(对话视图的目录随会话 purge 走;**老视图的文件在 agent 共用的 `agents/<key>/uploads/` 里,purge 不删、这里也不删,留在那里**);还有没到期的行指着的文件;大小 / sha256 与登记时不符的文件(没有 sha256 的老行先从登记时的 `DOCUMENT_UPLOAD` 审计行补,补不到只比大小);路径不可用的(某段是软链 / 不是目录、叶子不是普通文件,或用户子树里除暂时性以外的任何 I/O 错误:`chmod 000`、套接字……,计入 `upload_files_unsafe`)—— 以上几种行照样标 `deleted_at`;暂时性 I/O 失败的行(明天再试,计入 `upload_rows_failed`;过了到期日 7 天还在失败就只标行、不动文件,计入 `upload_rows_abandoned`);「放弃」(路径不可用、过期太久)先看完全批再标:涉及超过 3 个用户就一条都不标(`upload_rows_held`,像环境问题);有行被放弃或被拦下,pass 记失败;某个用户的 `{tenant}` / `{user}` 目录打不开(不是不在;没绑 agent 的会话以整个用户根为 `/workspace`,模型 `chmod` 得到)时只跳过这个用户这一轮的行、pass 记失败(`upload_owners_unavailable`),超过 3 个用户或工作区根本身打不开时整个 pass 中止、行不动 —— 以上都进 `failed_passes`,进程退出码 3;没有登记行的控制台上传与引用复制 |
| B-27 threads/ | `threads/<thread_id>/` 且 `thread_meta` 行不存在 | 行还在的目录(**不按时间删活会话**);非 UUID 名的子目录;已软删用户的整棵树 |
| X-4 ② 已删工作区 | 软删且已归档满 90 天的 `user_workspace` 行 + 该用户的 artifact / artifact_version / user_upload 行(`WORKSPACE_HARD_DELETE` 审计) | OSS 归档对象(桶生命周期到期);`.deleted/<user>` 标记 |

**不变式**(`tests/test_workspace_invariant.py`):对活着的工作区,job 只删
(a) 登记过的产物文件 (b) `uploads/` 下登记过的文件 (c) 孤儿 `threads/<id>/`;
根目录任何其它文件/目录 —— `style/`、`MEMORY.md`、未登记文件 —— 永不触碰。
碰文件的代码只有 `workspace_files.py` 的两个函数,没有任何「按时间扫目录删文件」。

会话 purge(`POST /v1/sessions/{id}:purge`)同步删 `threads/<id>/`;这里的
孤儿扫描只兜钩子失败与存量。
