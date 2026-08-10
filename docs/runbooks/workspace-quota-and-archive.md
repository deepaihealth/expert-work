# Runbook — 工作区配额与归档(workspace-quota-and-archive)

> 沙箱迁移波 3 交付(spec `docs/superpowers/specs/2026-08-09-sandbox-migration-w3-design.md`)。
> 覆盖:每用户工作区配额调整、删用户后的归档/恢复、OSS 生命周期与 NAS 快照一次性配置。
> supervisor / compose 路径不在本篇范围(冻结,行为照旧)。

## 机制速览

- **配额上限**:租户配额 `workspace_bytes_per_user` 维度;未配 = 平台默认 10 GiB。
  - 闸 A(领沙箱):`size_bytes >= limit`;闸 B(上传):`size_bytes + incoming > limit`。
  - 读、下载、删文件、列表永远放行(删文件是用户唯一自救路)。
- **记账三层**:上传增量 → release 60s 防抖 du → janitor 30 分钟全量扫。页面「已用」为约值。
- **删除收尾**:`user_purge` 写 `{tenant}/.deleted/{user}` 标记 → `WorkspaceJanitorWorker` 每轮:
  - 流式打包上传 OSS `workspace-archives/{tenant}/{user}/{workspace_id}.tar.gz`(确定性 key)
  - → `rm -rf` NAS 目录 → 行 `mark_archived`。标记文件保留(墓碑,挡 acquire)。
- **90 天**:`RetentionCleanupJob`(`workspace_archive_retention_days=90`)删档案对象 + 硬删行;
  OSS 生命周期规则是控制台兜底(见下)。误删自救窗口 = 90 天。

## 配额调整

- **管理界面**:设置 → 租户配额 → 维度选 `workspace_bytes_per_user`,limit_value 单位字节。
- **API**:`POST /v1/tenants/{tenant_id}/quotas`(upsert;curl 示例):

  ```bash
  curl -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "dimension": "workspace_bytes_per_user",
      "limit_value": 53687091200
    }' \
    "https://api.example.com/v1/tenants/<tenant-id>/quotas"
  ```

- **生效**:即时(闸每次读 store,无缓存)。

## 归档恢复(90 天窗口内)

1. **找 key**:

   ```sql
   SELECT id, archived_object_key FROM user_workspace 
   WHERE tenant_id='…' AND user_id='…';
   ```

2. **下载**:

   ```bash
   ossutil cp oss://<bucket>/workspace-archives/{tenant}/{user}/{workspace_id}.tar.gz ./a.tar.gz
   ```

   (S3 兼容形态):

   ```bash
   aws s3 cp --endpoint-url <endpoint> s3://<bucket>/… ./a.tar.gz
   ```

3. **解包回 NAS**:

   ```bash
   mkdir -p <nas_root>/{tenant}/{user} && tar -xzf a.tar.gz -C <nas_root>/{tenant}/{user}
   ```

4. **清墓碑**:

   ```bash
   rm <nas_root>/{tenant}/.deleted/{user}
   ```

5. **行复位**:

   ```sql
   UPDATE user_workspace SET deleted_at = NULL, archived_object_key = NULL WHERE id = '…';
   ```

6. **下一轮 janitor 全量扫会把 size_bytes 扫正(30 分钟内)。**

步骤 4/5 都完成前别让该用户领沙箱。

## 一次性配置

### OSS 生命周期规则(兜底删除)

控制台 → 目标 bucket → 数据管理 → 生命周期 → 新建规则:前缀 `workspace-archives/`,90 天后删除。
(应用层 `RetentionCleanupJob` 已做同样的事;此规则兜它挂掉的情况。)

### NAS 自动快照(取代云上每日全量备份)

NAS 控制台 → 快照 → 自动快照策略:每日一快照,保留 7 天,绑定 workspace 文件系统。

### 每日全量备份云上退役声明

云路径不跑 supervisor 的每日 volume 备份(该链路只在 compose/supervisor 形态存续)。
云上的恢复面 = NAS 快照(整树误操作)+ OSS 归档(单用户删除)。

## 已知窗口期与复活语义(硬要求②)

**PR-1 已上线、PR-2 未上线的窗口期**:

- 软删用户的上传**不入账**(增量记账对软删行早退)且无人清扫——字节滞留 NAS 直到 janitor 上线后第一轮收割。
- 上传路径与沙箱内 `write_file` **不查软删标记**(W2 既定设计):归档完成后目录仍可能被「复活」(purge 前已联入的会话/残存热沙箱)。janitor 每轮重新收割:**覆盖上传**同 key 档案 → 再删目录。覆盖意味着旧档案内容被复活内容替换——恢复操作要赶在复活写入前,或先下载核对档案内容。
- 复活面收敛:acquire 有软删闸(新沙箱拿不到);purge 完成后主体已删,上传 API 通常不可达——实际复活窗口 ≈ purge 前已联入的会话存续期。

## 故障排查

- **janitor 活着吗**:日志 `workspace_janitor.*`;多副本 advisory lock(classid 8619)单飞,
  loser 静默跳过是常态,不是故障。
- **单用户归档反复失败**:`workspace_janitor.archive_failed` 有堆栈;幂等重试,无 DLQ;
  连续多轮失败按堆栈修,手工补救走「归档恢复」逆操作。
- **配额显示与实际不符**:30 分钟粒度 + 同名覆盖上传重复计数是已知偏差,全量扫兜正。
