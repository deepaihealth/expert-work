# 沙箱迁移波 2 设计:工作区上 NAS + 技能搬出工作区(2026-08-07)

沙箱迁移总设计 `docs/superpowers/specs/2026-08-03-sandbox-migration-design.md` § 波 2 的落地设计。
上游依据:

- 存储选型与 W0 PoC:`docs/research/2026-07-28-storage-selection.md`(NAS 容量型定案,
  `csi-volume-config` 挂 NAS PV / subPath 隔离 / 跨 Pod 共享均已实测)
- W1 探针:`docs/research/2026-08-04-sandbox-w1-probe-results.md`(私有协议 / `user="agent"` /
  SDK 2.24.0 真实签名)
- 波 1 交付:PR #1101 + 遗留修复 12 条(`SandboxRuntime`/`WorkspaceStore` Protocol 已拆、
  8 个调用点已指 `workspace_store.*`、`build_workspace_store` 留了波 2 分支位)

## 一、官方文档新事实(2026-08-06 核对,改写波 2 前提)

来源:阿里云《在 ACS 集群中创建 Agent Sandbox》与《使用镜像缓存加速 ACS Pod 启动》(现行版)。

1. **`e2b.agents.kruise.io/csi-volume-config` 已官方文档化,且是"申请时动态挂载"**。
   经 `Sandbox.create(metadata={...})` 传 JSON,每挂载点配 `pvName` / `mountPath` /
   `subPath` / `readOnly`。"申请时"意味着**池领取的预建沙箱也能在领取瞬间挂上
   per-(tenant,user) 的 subPath**——W0 时代"未文档化黑科技"的最大不确定性已由官方口径消解,
   探针从"三机制探生死"降级为"验证官方配方"。
2. **ImageCache 无需申请开通**。现行文档通篇无"白名单/邀测"字样;工单只在"单地域配额
   默认 200,超了才提"一处出现。创建走控制台「镜像缓存」页或 OpenAPI(平台侧对象,
   非集群内 CRD——集群里没有 `eci.alibabacloud.com` API 组不说明任何问题)。
   每地域 20 个免费;使用费 0.00231 元/GiB/h 按 Pod 运行时长计,我们量级约 12 元/月。
   **勘误**:W1 探针报告《四、镜像缓存》一节"邀测需白名单/工单"为过时记录,本波 PR 顺手改正。
3. 沙箱池扩容默认吃镜像缓存(`ops.alibabacloud.com/update-with-image-cache` 默认
   `false` = 预热池扩容时使用缓存),SandboxSet 模板**零改**即受益。
4. 组件版本门槛:ack-sandbox-manager ≥ v0.6.0(集群实测 v0.6.8 ✓);
   ack-agent-sandbox-controller ≥ v0.5.14-release.1(托管组件,版本在控制台「组件管理」页,
   探针不通时先查它)。

## 二、决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | NAS 复用现存文件系统 `001qwl4r8snh205ihrs`(W0 PoC 建的,运行中,挂载点在测试集群 VPC) | 基建前置零工作;PoC 残留 PV(`nas-test-pv`)供探针直接用 |
| 2 | 数据根 `/workspaces/{tenant_id}/{user_id}/` | PoC 的 `/tenant-a/user-1` 是根上测试残渣,正式布局收进子树;探针跑完清残渣 |
| 3 | 沙箱挂载走 `csi-volume-config`(领取时动态挂载),SandboxSet 模板不动 | 官方配方(§ 一.1);模板级挂载做不了 per-user subPath |
| 4 | 技能文件搬到沙箱临时盘 `/opt/skills/<agent_key>/<skill>/...`,**per-agent 命名空间,不清理** | 见 § 四;"先清后写"在同用户双 agent 并发下会把运行中 agent 的技能整目录清掉——命名空间隔离后清理本身成为多余动作 |
| 5 | 用户工作区 `/workspace` **保持 per-(tenant,user) 共享,不按 agent 隔离** | 上传件要全 agent 可见(上传流程是 user 维度);跨 agent 接力(A 产出 B 分析)是场景不是事故;工作区浏览/配额/删用户级联全是 user 维度。同名文件 last-writer-wins 与本地后端今日行为一致 |
| 6 | 系统提示词加中间产物引导:中间文件写 `/tmp`,交付物写 `/workspace` | 降噪不改语义;`/tmp` 沙箱临时盘,重建即清、不占配额 |
| 7 | ImageCache 并入本波(控制台建缓存 + 实测),不再是外部依赖 | § 一.2,零工单零等待 |
| 8 | **沙箱镜像改造**:去 `USER agent`(容器 root 启动)、去预建 `/workspace` 与 `WORKDIR`、`HOME` 迁 `/home/agent`、预建 `/opt/skills` | § 二之二,集群实测锁定的两个真因 |
| 9 | **临时沙箱也挂 NAS**(`_scratch/<sandbox_id>` 子目录),不走"临时沙箱 cwd 改 /tmp" | 镜像不再预建 `/workspace` 后,不挂载的沙箱根本没有该目录;方案 B 会让两类沙箱行为分叉,违反本 spec 的反分叉原则。空目录清理并入波 3 扫描 job |
| 10 | **`PYTHONUSERBASE` per-agent**(exec 时注入 `/opt/agents/<agent_key>`) | 同用户双 agent 共享沙箱 ⇒ 共享 `$HOME/.local`;pip 装包互相覆盖 + 并发损坏。该变量同时决定 `pip --user` 落点与 import 路径,per-agent 即隔离,零镜像改动 |

## 二之二、集群实测:挂载失败的两个真因(2026-08-07)

工单白名单批复后挂载仍失败,控制器做了对照实验(同集群/同 PV/同注解,只换镜像):

| 配置 | 结果 |
|---|---|
| 官方 `code-interpreter` 镜像 | **挂载成功**,0.3s 池领取,读写正常 |
| 我们镜像(`USER agent`,uid 10000) | `fork/exec /mnt/envd/sandbox-runtime-storage: operation not permitted` |
| 我们镜像 + pod `runAsUser: 0` | 错误变为 `process error: exit status 1`(helper 已能执行) |
| 我们镜像 + root + `mountPath=/mnt/ws` | **成功**,写入读回正常 |
| 我们镜像 + 非 root(uid 10000)+ gid 0 | 仍 EPERM —— gid 0 不够,必须 root |

**真因一:容器必须以 root 启动。** envd 与容器同身份,要 fork/exec 存储 helper(helper 本身是 `rwxr-xr-x`,非文件模式问题)。官方镜像不设 `USER`,容器 root,再由 envd 用 `commands.run(user=...)` 降权执行——正是 W1 已在传 `user="agent"` 的机制。我们把容器身份也锁成非 root,顺带锁死了平台自己的活。安全上不亏:ACS 侧隔离边界是 microVM 而非容器用户;本地 docker 侧容器仍是边界,靠 `docker run --user 10000:10000` 保住非 root。

**真因二:`mountPath` 在镜像里不能预先存在。** 平台是在该路径**建 symlink** 指向 `/run/csi/mount-root/nas/<hash>`,不是往目录上挂。我们镜像预建了 `/workspace`(且 `HOME`/`WORKDIR`/`MPLCONFIGDIR` 都指它)。注意 `WORKDIR` 指令**本身也会创建目录**,只删 `mkdir` 不够。

附带事实:NAS 新建子目录属主 root,非 root 用户写入被拒 → acquire 前 mkdir 要连带 chown(决策见 § 五之二);pod 级 `securityContext` 会波及 sidecar(实测 csi-agent-sidecar CrashLoop),只能用容器级;`commands.run(user="root")` 被平台拒(`InvalidArgumentException`),沙箱内没有 root 兜底路径。

## 二之三、沙箱内最终布局

| 路径 | 存储 | 内容 | 生命周期 |
|---|---|---|---|
| `/workspace` | NAS(平台建的 symlink) | 用户数据:`uploads/` + agent 产出 | 永久,跨沙箱重建 |
| `/opt/skills/<agent_key>/<skill>/` | 沙箱本地盘 | agent 技能文件 | 沙箱重建即清,不占配额 |
| `/opt/agents/<agent_key>/` | 沙箱本地盘 | `PYTHONUSERBASE` —— pip `--user` 装的包 | 同上 |
| `/home/agent` | 沙箱本地盘 | matplotlib 配置、各类 cache | 同上 |
| `/tmp` | 沙箱本地盘 | exec 临时脚本、中间产物 | 同上 |

用户 NAS 目录里只有用户自己的数据;技能、pip 包、缓存、中间产物全在本地盘。

## 三、挂载布局与基建

```
NAS 001qwl4r8snh205ihrs
└── /workspaces/                      ← 数据根
    └── {tenant_id}/{user_id}/        ← 每用户工作区(权威)

消费者 1:control-plane Pod(整棵树)
  PV workspace-nas(NFS server=挂载点, path=/workspaces, RWX, Retain)
  + PVC(expert-work ns) → Deployment volumeMount /mnt/workspaces
  → NasWorkspaceStore 直接文件系统读写
  manifest 进 infra/k8s/base/

消费者 2:沙箱(单用户子树)
  acquire → Sandbox.create(metadata={"e2b.agents.kruise.io/csi-volume-config":
    '[{"pvName": "workspace-nas", "mountPath": "/workspace",
       "subPath": "<tenant_id>/<user_id>"}]'})
  → 沙箱内 /workspace = NAS 上该用户子树,NFS 共享,control-plane 即时可见
```

- PV 同时被 PVC Bound(control-plane)与 `csi-volume-config` 引用(沙箱):
  W0 PoC 已实证这种"一女二嫁"可行(`nas-test-pv` 当时即 Bound 状态被沙箱挂上)。
- `subPath` 的准确语义(相对 NAS 根还是相对 PV path;文档措辞"绝对地址")由探针定,
  两种答案都只影响 manifest/常量取值,不影响结构。
- 新用户目录不存在时挂载的行为由探针定;若不自动建,`AgentSandboxClient.acquire`
  在 create 前经本 Pod 挂载点 `mkdir -p`(control-plane 挂着整棵树,顺手)。

## 四、技能文件:`/opt/skills/<agent_key>/`,per-agent 命名空间

总设计已拍板技能搬出工作区(它是 agent 的能力定义,不是用户数据;不该占 NAS 配额、
不该出现在用户的工作区浏览里)。本波定实现形态:

- **目标路径** `/opt/skills/<agent_key>/<skill-name>/...`;`agent_key` = agent 的
  manifest 名(`spec.metadata.name`,构建期的稳定标识——DB UUID 在构建作用域里
  不存在)经字符白名单清洗(`[^a-zA-Z0-9._-]` → `-`);系统提示词里的技能路径按 agent 插值。
- **命名空间在构建期拼进 relpath 前缀**(builder 组装 `skill_seed_files` 时),
  `acquire(seed_files=...)` 签名零改、Protocol 零改。
- **不清理**。并发安全靠命名空间(两个 agent 各写各的子树);同一 agent 重复 acquire
  重 seed 相同 bytes,幂等。目录随沙箱重建自然消失,占的是临时盘。
- 两后端同步改,契约测试同断言(总设计明令禁止行为分叉):
  - 云(microVM):seed 落点从 `{WORKSPACE_ROOT}/{relpath}` 改
    `{SKILLS_ROOT}/{relpath}`(`agent_sandbox.py` 一处)。
  - 本地(docker):rootfs 只读、`/workspace` 是现在唯一可写处 → `docker run` 给
    `/opt/skills` 加一块 tmpfs(uid 对齐镜像的 agent 用户 10000);
    supervisor seed 落点根从 `/workspace` 改 `/opt/skills`——seed 机制是
    `docker exec <container> tar -xf - -C <root>`(正是为绕开 docker cp
    拒写 tmpfs 才选的),对 tmpfs 目标已被现状证明(ephemeral `/workspace`
    本来就是 tmpfs),零兼容风险。
  - supervisor 冻结原则在此破一个口:总设计波 2 明文要求两后端同步搬,冻结让位于既定决策。
- 提示词/文档里所有 `/workspace/skills` 引用全库 grep 改 `/opt/skills/<agent_key>`。

### 并发语义盘点(同用户双 agent,共享同一沙箱)

| 层 | 行为 | 状态 |
|---|---|---|
| acquire 竞争 | 部分唯一索引 + CAS 单赢家,输家 connect;两边各自 seed 自己的命名空间 | 已测 |
| exec 并发 | 本地 per-sandbox 锁串行(held-pipe 机制所迫);云无锁并行,临时脚本 `/tmp/ew-exec-<uuid4>.py` 不撞名 | 分叉是机制性非语义性,契约偏差记档 |
| `/workspace` 共享写 | 同文件 last-writer-wins | 既定语义(决策 5) |
| 技能 seed | per-agent 子树互不相扰 | 本波修(决策 4) |
| egress token | per-sandbox 非 per-run,共享无害 | 已对齐(W1 N-3) |

## 五之二、并发反思:同一用户同时用多个 agent

热会话是 per `(tenant, user)` 不含 agent ⇒ 同一用户的两个 agent **共享同一个沙箱**。逐目录核过:

| 面 | 并发行为 | 处置 |
|---|---|---|
| 技能 seed | `acquire` 的 seed 循环在复用/新建两分支**之外**(代码核实),后来者连热会话也会投递自己的技能;per-agent 命名空间不互踩 | 无需改动 |
| `/workspace` | 同名文件 last-writer-wins | 既定语义(决策 5) |
| exec 临时脚本 | `/tmp/ew-exec-<uuid4>.py` 天然不撞 | 无需改动 |
| **`$HOME/.local`(pip)** | **两 agent 共享 ⇒ 装包互相覆盖、并发装可能损坏目录** | 决策 10:`PYTHONUSERBASE` per-agent |
| egress token | per-sandbox 非 per-run | W1 已对齐 |

## 五、NasWorkspaceStore

- 实现 `WorkspaceStore` Protocol 全部 5 方法(read_file / list_files / write_file /
  delete_file / mark_deleted),直接文件系统(pathlib),root 走新配置项
  `workspace_nas_root`(control-plane Pod 内挂载点路径,如 `/mnt/workspaces`)。
- `mark_deleted` 语义与 `SupervisorWorkspaceStore` 逐条对齐(plan 阶段核对
  supervisor 的 marker 实现,byte-level 同义——SQL↔内存 store 谓词同义的老教训)。
- 工厂 `build_workspace_store`:配了 `workspace_nas_root` → `NasWorkspaceStore`;
  否则有 supervisor url → `SupervisorWorkspaceStore`;都无 → `None`(现有降级路径)。
- **路径穿越防护跟着搬**(总设计 § 七.1 原文):`path` 来自 API 入参,规范化后校验
  解析结果仍在 `{root}/{tenant_id}/{user_id}/` 子树内,拒符号链接逃逸。
  针对性测试四件套:`../`、绝对路径、符号链接、URL 编码变体,先红后绿。
- 波 1 刻意欠的账就此还清:8 个调用点(workspace/sessions/artifacts/uploads/user_purge)
  已指 `workspace_store.*`,云上从 `None` 变 `NasWorkspaceStore` → 文件浏览 / 上传 /
  产物下载 / 删用户级联全通;上传落 NAS 后沙箱经 NFS 即时可见,`read_document` 直读。
- control-plane 挂整棵树的权限半径变化总设计 § 七.2 已记录在案,本波不重复裁决。

## 六、探针任务(开局,产出入 `docs/research/`,照 W1 先例)

1. `csi-volume-config` 官方配方验证:池领取路径挂现存 `nas-test-pv` →
   `/workspace` 可写;`subPath` 语义;新用户目录不存在时的行为;钉死的 SDK 2.24.0
   `create(metadata=...)` 透传无损。
2. ImageCache:控制台对 `expert-work/sandbox:<sha8>` 建缓存(VPC 内网、同账号 ACR 免密)
   → 删池内沙箱触发补池,实测就绪耗时(基线 35~40s,官方称可到秒级)。
3. 顺带:勘误 W1 探针报告 ImageCache 一节(§ 一.2);清 NAS 根上 PoC 残渣。

降级分支(探不通才走):机制不通 → 查 ack-agent-sandbox-controller 版本并升级重试;
仍不通 → 用户沙箱放弃池领取、standalone 冷建(`create-on-no-stock: true` 减等待,
ImageCache 压冷启),热会话使冷建只发生在用户首次 acquire。

## 七、测试

照总设计 § 九三档框架:

- **单测**:`NasWorkspaceStore` 用 `tmp_path` 真文件系统测(文件系统实现,零 mock);
  路径穿越四件套;技能命名空间 relpath 前缀的构建期逻辑。
- **契约**:`WorkspaceStore` 一套用例两实现参数化(Supervisor 侧真跑 docker,
  Nas 侧 `tmp_path` 本地即测);技能 seed 落点 `/opt/skills/<agent_key>/`
  两后端同断言;`mark_deleted` 行为同义断言。
- **真集群契约**:现有 e2b 契约 workflow 加挂载用例——create 带 `csi-volume-config`
  → exec 写 `/workspace` → 断言 NAS 侧同路径读到同一内容(CI 内以第二个沙箱挂同
  subPath 验证共享,不依赖 CI runner 挂 NFS)。

## 八、端到端验收(测试集群真栈,一条链)

> 前端上传文档 → agent `read_document` 读到内容 → agent exec 写出结果文件 →
> 前端工作区浏览看到 + 下载成功 → 技能文件**不出现**在工作区浏览 →
> 销毁沙箱重建后文件仍在(工作区权威在 NAS 坐实)

同场并跑还挂着的 **W1 Task 11 验收**(agent 真跑 `exec_python` 出结果,出网经
credential-proxy 且审计落 `sandbox_egress_audit` 表),一次真栈跑完两波的账。

## 九、发布面

- control-plane Deployment 加 PV / PVC / volumeMount(base manifest + overlay 照常);
- SandboxSet **零改**(挂载是领取时 metadata,ImageCache 是平台侧对象);
- 无 DB 迁移;
- 新配置项 `workspace_nas_root` 进 overlay configmap;本地 compose 不配它
  (docker 后端继续走 supervisor 路径)。

## 十、明确不做

- 不做工作区按 agent 隔离(决策 5)。
- 不做配额扫描 job / NAS 原生目录配额查证 / 归档搬 NAS / 快照策略——波 3(总设计 § 波 3)。
- 不做休眠 / 唤醒语义与前端反馈——波 3。
- 不动 credential-proxy、不动 exec 契约、不动热会话 CAS。
- 不给 exec 并发加跨副本锁(本地锁是 held-pipe 机制所迫,云端 envd 天然支持并行;
  语义层面工作区本就 last-writer-wins)。
