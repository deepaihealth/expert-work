# 沙箱迁移波 3 设计:工作区配额 + 删除收尾(2026-08-09)

沙箱迁移总设计 `docs/superpowers/specs/2026-08-03-sandbox-migration-design.md` § 波 3 的落地设计。

## 〇、范围重裁(相对总设计 § 波 3,已与用户逐条对齐)

| 总设计条目 | 本波处置 | 依据 |
|---|---|---|
| 热会话映射改 DB | **不做——已交付**(波 1:`sandbox_instance` 表 + 迁移 0141 CAS 唯一索引) | 摸底核对 |
| `reap` 云实现 | **不做——已交付**(W1 遗留修复:`SandboxReapWorker`,240s 一扫,空闲 15 分钟杀,多副本幂等) | 摸底核对 |
| 休眠/唤醒 + 前端反馈 | **裁掉,记 backlog**(用户拍板 2026-08-09) | 空闲释放省成本这一目标已由 kill 达成(零计费);工作区在 NAS 文件不丢;池命中亚秒比唤醒(1–10s)还快。剩余价值只有沙箱本地进程态/pip 包,不值一整条未验证链路(pause 在 ACS 侧从未验证,`agent_sandbox.py` 有明文警告) |
| 工作区配额 | **本波主菜**(云路径现状零配额) | 用户拍板:拦领沙箱 + 拦上传 |
| 归档改从 NAS 打包 | **本波主菜**(软删标记现状无人消费,字节永久滞留) | 用户拍板:先归档 OSS 再删,90 天生命周期 |
| 每日全量备份退役 → NAS 快照 | 做(纯运维件:runbook + 声明) | 云上本来就没跑过每日备份 |
| NAS 原生目录配额(SetDirQuota)查证 | 做(仅调研结论入仓,不接线) | 总设计既定「并行查证」定位 |
| `_scratch` 清理 | 做(波 2 spec 决策表第 9 条明文欠账) | |

**沙箱镜像、SandboxSet、supervisor 路径零改动**(supervisor 冻结原则延续)。

## 一、问题(具体场景)

1. **配额洞**:`QuotaEnforcer` 只活在 sandbox-supervisor 里,云上不部署该服务。任何用户让 agent 反复生成大文件,写满整个 `workspace-nas` 文件系统 → **全部租户全部用户**一起 ENOSPC 停摆。上传路径(control-plane 直写 NAS)同样无闸。
2. **删除收尾洞**:删用户(`user_purge`)只在 `{tenant}/.deleted/` 写标记文件;无任何消费者。被删用户的字节永久躺在 NAS 上计费,「删用户」名不副实。
3. **欠账**:`_scratch/<sandbox_id>`(临时沙箱)目录无人清;老归档路径带 1.5GiB 内存 buffer 上限(`archive_volume(max_bytes=…)`),云路径不复用它。

## 二、已核对的代码事实(设计的地基)

- `user_workspace` 表全套现成:`size_bytes` / `size_limit_bytes`(列默认 10GiB)/ `deleted_at` / `archived_object_key`,store 方法 `resolve`(幂等 upsert)/ `update_size` / `soft_delete` / `mark_archived` / `list_pending_archive` / `list_active` / `hard_delete` 全在。**云路径从不调用**——`resolve()` 全仓唯一调用方是 supervisor(+ 它的 lifecycle)。
- 迁移 0026 docstring 写的「manifest 可用 `policies.workspace_size_limit_mb` 覆盖」**从未实现**(无活代码)。本波不补。
- 租户配额系统现成:`tenant_quota` 表 + `QuotaDimension` 枚举 + `/v1/tenants/{t}/quotas` CRUD + 管理界面 `SettingsTenantQuotas.tsx`。同形状先例:`IMAGE_STORAGE_BYTES`(字节黏性上限,refill 0)。
- 软删标记:`workspace_deleted_marker()` → `{root}/{tenant}/.deleted/{user_id}`;唯一消费者是 `AgentSandboxClient.acquire` 的软删闸(拒绝领沙箱)。`user_purge` 的 workspace 步骤已接 `NasWorkspaceStore.mark_deleted`(写标记 + 拆热会话)。
- 临时沙箱(无 `user_id`)目录:`{root}/_scratch/{sandbox_id}`(`agent_sandbox.py:212`),寿命 ≤ 平台 20 分钟上限。
- `ObjectStore` 协议 5 操作:`put(bytes)` / `get` / `delete` / `list_prefix` / `presigned_url`——**没有流式写**。
- 异常先例:`WorkspacePermissionError(SandboxSupervisorError)` 定义在 `orchestrator/tools/sandbox.py:134`,control-plane 端点按「子类先于父类」顺序 except。
- 后台 worker 先例:`SandboxReapWorker`(无锁多副本幂等)与 `QualityDriftWorker`(advisory lock 单飞,因为有重副作用)。

## 三、配额

### 3.1 上限来源(单一来源,不做两处合并)

```
effective_limit(tenant) =
    tenant_quota[dimension=WORKSPACE_BYTES_PER_USER].limit_value   # 租户配了就用
    ?? DEFAULT_WORKSPACE_BYTES_PER_USER (= 10 GiB, 共享常量)        # 没配走默认
```

- 新增 `QuotaDimension.WORKSPACE_BYTES_PER_USER = "workspace_bytes_per_user"`。语义:**每用户**工作区字节上限(租户内所有用户同一上限)。它是**存储型上限**,只作为配置值被读取,**不接** Redis 令牌桶/admission 链路(与 QPS 类维度的本质区别;spec 明示,防止实现时顺手接错)。
- `user_workspace.size_limit_bytes` 列**云闸不读**,留给 supervisor 冻结路径。列保留,不迁移不删。
- 管理入口 = 既有租户配额页加一个维度选项(双语文案 + GiB 单位格式化),CRUD 走既有端点,零新端点。

### 3.2 记账(谁知道用了多少)

`size_bytes` 权威在 `user_workspace` 行,云路径开始 `resolve()` 建行。三个更新时机,从即时到兜底:

1. **上传成功后增量入账**:`uploads.py` 写完 NAS 后 `add_size(workspace_id, +len(raw))`。新增 store 方法 `add_size`(原子 `UPDATE size_bytes = size_bytes + delta`),SQL 与 in-memory 两实现**谓词逐字节同义**(仓库既有铁律)。同名覆盖上传会重复计数——已知偏差,靠 3 兜正,不做减法补偿。
2. **release 后重算**:agent 在沙箱里写的字节靠这个入账。`AgentSandboxClient.release` 结尾 fire-and-forget 调闸对象的 `refresh(tenant_id, user_id)`(镜像 supervisor `QuotaEnforcer.check`/`refresh_size` 的形状)——对该用户目录 `os.scandir` 递归求和(`lstat`,不跟符号链接),写回 `update_size`。在 `asyncio.to_thread` 里跑,失败吞掉(下轮全量扫兜底)。**注意 release 的真实频率**(已核对 `sandbox.py:589`):每次沙箱工具调用后都触发,不是 run 结束一次——所以 `refresh` 带 per-`(tenant, user)` 进程内防抖(60s 内重复触发直接跳过),防止话痨 run 用 du 锤 NFS 元数据。
3. **janitor 周期全量扫**(§ 五):30 分钟一轮,逐用户目录求和写回。兜住一切漂移(带外写入、增量计数偏差、release 刷新失败)。

### 3.3 两道闸

**闸 A——领沙箱**(拦 agent 继续产出):

- `AgentSandboxClient` 注入可选 `quota_gate`(orchestrator 侧定义 Protocol,control-plane 组装实现,依赖 `UserWorkspaceStore` + `TenantQuotaStore` + 默认常量;方向与既有 store 注入一致,orchestrator 不 import control-plane)。
- `acquire` 在波 2 软删闸旁、`claim_warm` 之前查:`row.size_bytes >= effective_limit` → 抛新异常 `WorkspaceQuotaExceededError(SandboxSupervisorError)`(定义在 `orchestrator/tools/sandbox.py`,挨着 `WorkspacePermissionError`;与 supervisor 服务内同名异常无关,互不 import)。
- control-plane 映射 429,detail 指向自救路径(「工作区已满,请在工作区页清理文件」)。对话内表现为工具报错,文案人话。
- 临时沙箱(`user_id is None`)不查——无用户工作区,写 `_scratch`,由生命周期(≤20 分钟)+ janitor 清理兜底。
- `quota_gate` 未注入(如本地 compose 或测试)→ 行为与今天完全一致,零闸。

**闸 B——上传**(拦前端塞文件):

- `uploads.py` `_handle_document_upload` 写 NAS 前查:`row.size_bytes + len(raw) > effective_limit` → 429(上传知道 incoming 大小,用「写完会不会超」;闸 A 不知道未来写多少,用「已超才拦」——两个谓词刻意不同,spec 明示防被"统一")。
- except 顺序:`WorkspaceQuotaExceededError` 排在 `SandboxSupervisorError` 宽 except 之前(W2 `WorkspacePermissionError` 同款教训)。

**永远放行**:读、下载、**删文件**(用户唯一的自救路)、工作区列表。

## 四、删除收尾(归档清扫)

### 4.1 流程

janitor(§ 五)每轮扫各租户 `.deleted/` 目录,对每个标记:

1. `resolve()` 行(建行兜住「从没建过行就被删」的老用户)→ 行未软删则 `soft_delete`。
2. 用户目录**流式打包**上传 OSS:key **确定性** = `workspace-archives/{tenant_id}/{user_id}/{workspace_id}.tar.gz`(workspace_id = 行 UUID;重试自然幂等覆盖,不产生重复档案)。
3. 上传成功后 `rm -rf` NAS 用户目录。
4. `mark_archived(key)`。
5. **标记文件保留**(空文件即墓碑):`acquire` 软删闸靠它继续拒绝,防「归档完又被领沙箱重建目录」的僵尸工作区。

**崩溃安全顺序**:先传后删,`mark` 最后。重入规则:标记存在且行未 `archived` 时——目录在 → 从第 2 步重做(覆盖上传);目录不在 → `list_prefix` 探 OSS,对象在 → 直接 `mark_archived`(上次删完没来得及 mark),对象不在 → 上传**空 tar.gz** 再 mark(用户生前就没有目录,统一产出档案,恢复侧无需分叉)。行已 `archived` → 跳过,零操作。

### 4.2 流式打包(1.5GiB 上限死在这里)

- `ObjectStore` 协议新增 `put_stream(key, chunks: AsyncIterator[bytes], *, content_type)`;S3 实现走 multipart upload(底层 boto 客户端已在),in-memory 实现攒 bytes(测试用,行为契约一致)。
- tar.gz 生成:`tarfile` 写入管道式 fileobj,`asyncio.to_thread` 里跑打包线程,async 侧按分片(64 MiB)喂 multipart——常驻内存 ≤ 单分片,**无总量上限**。
- 老 `archive_volume(max_bytes=…)` 及其 1.5GiB 语义不动(supervisor 冻结),云路径不引用。

### 4.3 OSS 90 天生命周期

归档桶前缀 `workspace-archives/` 配 OSS 生命周期规则「90 天后删除」——**控制台运维配置**,不写应用层硬删代码。runbook 给步骤,验收查规则存在。误删自救窗口 = 90 天,窗口内恢复 = 下载档案解包回 NAS(runbook 给命令)。

## 五、janitor —— `WorkspaceJanitorWorker`

control-plane 内新后台 worker,30 分钟一轮,每轮三阶段(顺序固定):

1. **归档清扫**(§ 四)——先清死的,免得阶段 2 白量将删目录。
2. **配额全量扫**:遍历 NAS 根 `{tenant}/{user}` 目录(文件系统为发现源,行不存在就 `resolve` 建;跳过 `.deleted`、`_scratch`),逐个求和 `update_size`。
3. **`_scratch` 清理**:`{root}/_scratch/<sandbox_id>` 目录 mtime 距今 > 24h → 删。临时沙箱寿命 ≤ 20 分钟,24h 是 72 倍安全余量;mtime 判据不查 DB,清扫与沙箱状态解耦。

- **advisory lock 单飞**(`QualityDriftWorker` 先例):归档上传是重副作用,两副本并发做同一用户会互踩(上传撞 multipart、`rm -rf` 撞遍历)。锁拿不到 → 本轮跳过,静默。
- 每阶段单目录/单用户失败:log + 继续下一个,下轮自然重试。**不建 DLQ**(supervisor 的 `VolumeBackupDLQ` 是它的架构需要;janitor 幂等 + 周期重试已够,失败可观测靠 log/告警)。
- 停机语义照 `SandboxReapWorker`:`stop()` 短超时,超时取消,幂等下轮重来。

## 六、前端(两小件)

1. **上传 429**:workspace 满时的上传失败 toast 用明确文案「工作区已满,请清理文件后重试」(i18n 双语;区别于泛化的"上传失败")。
2. **用户运维页工作区 tab**:加「已用 / 上限」一行(数据源 = `user_workspace` 行 + effective_limit,由既有用户工作区端点响应顺带携带;扫描 30 分钟粒度,页面标注「约」)。

租户配额页的维度选项 + 文案 + GiB 格式化算配额链一部分(§ 3.1),不重复列。

## 七、运维件

- **runbook 新篇**(`docs/runbooks/workspace-quota-and-archive.md`):配额调整操作(租户配额页/API)、归档恢复步骤(OSS 下载 → 解包回 NAS → 清标记 → 行复位)、OSS 生命周期规则配置步骤、NAS 快照策略配置步骤(控制台,建议每日一快照保 7 天)、「每日全量备份云上退役」声明(supervisor compose 路径不变)。
- **SetDirQuota 调研结论**入 `docs/research/`:在我们的 CSI 挂载方式下可行性、500 配额目录上限对租户×用户规模的含义、要不要作为二道硬闸叠加。**只出结论不接线**;结论为可行时开 backlog 项。

## 八、测试策略

- **store 层**:`add_size` SQL/in-memory 谓词同义(真容器集成测,含并发增量原子性);`resolve` 建行路径回归。
- **闸**:acquire 超限 429 端到端(经真 `run_agent` 工具路径的报错渲染)、上传超限 429、删文件后恢复、`quota_gate` 未注入零行为变化、临时沙箱不受闸。
- **janitor**:tmpdir 假 NAS 树上三阶段全覆盖;归档崩溃重入矩阵(传后崩/删后崩/mark 前崩 × 目录在/不在 × OSS 对象在/不在);`_scratch` mtime 边界;advisory lock 双实例单飞。
- **变异自证**(仓库铁律):每条新断言 break→red→restore→green;重点杀「闸读错上限来源」「谓词 `>=` vs `>` 互换」「归档 key 不确定性」。
- **契约测试**:`put_stream` 进 ObjectStore 契约档(S3/memory 双实现);workspace store 契约补 `add_size`。
- **真栈验收**(集群):写满配额 → 上传 429 + agent 领沙箱报「工作区已满」→ 删文件恢复;删用户 → OSS 出现档案、NAS 目录消失、janitor 重跑不重复归档;`_scratch` 老目录被清活目录无伤;租户配额页改上限 → 闸即时生效。

## 九、PR 切分(预估 2 个)

1. **PR-1 配额链**:维度 + 默认常量 + `add_size` + `resolve` 接线 + `quota_gate`(Protocol/实现/注入)+ 闸 A/B + 429 映射 + release 刷新 + 租户配额页维度选项 + 上传 429 文案。
2. **PR-2 janitor + 收尾**:`put_stream` + `WorkspaceJanitorWorker` 三阶段 + 用户运维页已用/上限 + runbook + SetDirQuota 调研文档。

依赖方向:PR-2 依赖 PR-1 的行建立与 `add_size`;PR-1 先行时闸已可用(记账靠增量 + release 刷新,全量扫兜底晚 30 分钟到位,可接受)。

## 十、明确不做

- 休眠/唤醒(backlog;先决条件 = ACS pause/resume 集群实测)。
- SetDirQuota 接线(仅调研)。
- 租户级聚合配额(所有用户共享一个池)——每用户上限已挡住「单人写满 NAS」。
- 每用户单独上限覆盖(行列留着,真有需求再开管理面)。
- manifest `policies.workspace_size_limit_mb`(从未实现,继续不实现)。
- 沙箱内实时写入拦截(运行中任务半路炸,用户已否)。
- 90 天后档案的应用层硬删(OSS 生命周期规则替代)。
- supervisor / compose 路径任何行为变化(冻结)。

## 十一、与其他工作的关系

- 波 2 的软删标记语义、`WorkspacePermissionError` 分层、统一 uid 前提全部沿用不动。
- `user_purge` 零改动(workspace 步骤已写标记;janitor 是新增的异步消费者)。purge 响应文档补一句「字节归档异步完成」。
- 总设计 § 波 4(死字段裁决、沙箱指标、契约补全、文档)不动,仍是下一波。
