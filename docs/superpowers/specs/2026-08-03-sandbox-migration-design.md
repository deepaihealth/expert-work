# 沙箱/工作区多节点迁移设计(2026-08-03)

部署改造审计**第 3 波**的设计文档。上游依据:

- 审计原文 `docs/research/2026-07-28-multi-replica-readiness-audit.md` § 第 3 波
- 沙箱运行时选型 `docs/research/2026-07-28-agent-sandbox-selection.md`(ACS Agent Sandbox + E2B SDK 已拍板)
- 存储选型 `docs/research/2026-07-28-storage-selection.md`(工作区 = 通用型 NAS 容量型,W0 PoC 已实证)

选型层已收口,本文只做**方案层**:抽象怎么切、波次怎么排、关键机制怎么定。

## 一、问题

现架构在云集群上跑不了,两条硬伤:

1. `sandbox-supervisor` 挂宿主 `docker.sock`,只能管本机(`docker_client.py` 走 CLI 子进程,无 `DOCKER_HOST` 参数化);control-plane 只配单个 supervisor URL;`sandbox_instance.node` 写死 `"local"` 且无人查询。
2. 用户工作区 = 宿主本机 Docker 卷(`expert-work-ws-{tenant}-{user}`),run 换节点即数据分叉成两份空卷;热会话表 / exec 锁 / warm pool 全在 supervisor 进程内存。

附带三个既有疮:reaper 与每日备份每副本各跑、无选主,会跨节点误杀容器 / 重复归档;归档单发 buffer 上限 1.5GiB < 10GiB 工作区配额,超限永远失败进 DLQ;`spec.sandbox` 15 字段 13 个是死的。

**当前测试集群里根本没有 sandbox-supervisor**(`infra/k8s/base/` 无此服务),所以这波是"从零往集群里放沙箱",不是替换在跑的东西。

## 二、迁移的核心利好:接缝已存在

orchestrator 与 control-plane **全部**经 `SupervisorClient` Protocol(`services/orchestrator/src/orchestrator/tools/sandbox.py:126-198`)调沙箱,单点工厂 `services/control-plane/src/control_plane/runtime.py:1448 build_supervisor_client(url)`,`url=None` 时整个沙箱能力优雅关闭(agent 声明沙箱工具则构建期失败并报错)。

`exec_python` / `bash` / `read_file` / `write_file` / `edit_file` / `list_dir` / `read_document` 六类工具 + workspace-ingest 全走该 Protocol。**迁移 = 新写一个实现**,上层工具零改。

## 三、已拍板决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | **双实现并存**,supervisor 冻结 | 本地 / CI 保留 docker supervisor(3483 行测试 + `sandbox-gvisor.yml` 验收套件继续有价值,本地能离线调沙箱工具);云上走 `AgentSandboxClient`。supervisor **冻结**:不再加新功能,只维持契约兼容,新能力只在云实现上做 |
| 2 | 工作区文件操作**拆成独立 `WorkspaceStore`** | PoC 已证 control-plane 能跨 Pod 直读同一 NAS;文件浏览 / 上传 / 下载不该要求沙箱在场 |
| 3 | 配额:**先软配额 + 后台扫描,并行查 NAS 原生目录配额** | 见 § 六。硬配额路线(per-user PVC)的 500 目录上限对几千用户目标是硬伤 |
| 4 | `credential-proxy` **原样保留** | 它跟沙箱运行时正交(沙箱只是 `HTTPS_PROXY` 客户端);秘密注入是 per-tenant 动态的,平台的声明式凭据注入替代不了;审计要落 `sandbox_egress_audit` 表 |
| 5 | 归档 / 备份:**快照接管每日备份,删用户归档保留** | 快照是全盘的,没法 per-user 取回,替代不了"删用户 → 归档 → 90 天硬删"这条合规链;每日全量备份在 NAS 上是重复建设 |

## 四、抽象与边界

### 4.1 Protocol 一拆二

现在一个 `SupervisorClient` 背两件事。拆开:

```python
@runtime_checkable
class SandboxRuntime(Protocol):
    """沙箱运行时 —— 一个沙箱实例的生命周期与执行。"""

    async def acquire(
        self, *, tenant_id: UUID, thread_id: str, user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID: ...

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None
    ) -> SandboxOutcome: ...

    async def release(self, *, sandbox_id: UUID) -> None: ...
    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None: ...
    async def reap(self, *, force: bool) -> int: ...


@runtime_checkable
class WorkspaceStore(Protocol):
    """用户持久工作区的文件操作 —— 与沙箱是否在场无关。"""

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes: ...
    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> list[WorkspaceFileEntry]: ...
    async def write_file(
        self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes
    ) -> None: ...
    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None: ...
    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None: ...
```

**改名**:`SupervisorClient` → `SandboxRuntime`。Agent Sandbox 下没有 supervisor 了,旧名字会长期误导。纯机械 diff,趁引用面最小时改(波 1)。

### 4.2 四个实现

| 抽象 | 本地 / CI | 云上 |
|---|---|---|
| `SandboxRuntime` | `HTTPSupervisorClient`(现有,冻结) | `AgentSandboxClient`(E2B SDK,新写) |
| `WorkspaceStore` | `SupervisorWorkspaceStore`(HTTP 转 supervisor —— docker 卷只有它碰得到) | `NasWorkspaceStore`(直接文件系统,`{root}/{tenant_id}/{user_id}/`) |

### 4.3 工厂

```python
def build_sandbox_runtime(settings: Settings) -> SandboxRuntime | None:
    """None → 沙箱工具不可用(现有降级路径,不变)。"""
    if settings.sandbox_backend == "agent_sandbox":
        return AgentSandboxClient(...)
    if settings.sandbox_supervisor_url is not None:
        return HTTPSupervisorClient(base_url=settings.sandbox_supervisor_url)
    return None
```

新配置项 `sandbox_backend: Literal["supervisor", "agent_sandbox"] | None = None`。
`WorkspaceStore` 同形态一个工厂,按同一个开关选。

### 4.4 调用点改指

`WorkspaceStore` 拆出后,control-plane 侧 8 个调用点从 `supervisor.*` 改指 `workspace_store.*`:

- `api/workspace.py:179 / 224 / 265`(浏览 / 读 / 删)
- `api/sessions.py:498 / 550 / 596 / 651`(会话内工作区面板)
- `api/artifacts.py:218`(产物下载)
- `api/uploads.py:158`(文档上传落地)
- `purge/user_purge.py:440`(删用户级联)

## 五、波次

每波以"测试环境能真跑通某件事"收尾。三个未验证项全压在波 1。

**一波一个实施计划** —— 本 spec 覆盖四波,但 writing-plans 阶段按波出计划,不攒成一份。波 1 的实测结论会改写波 2-4 的细节,提前写死是浪费。

### 波 1 — 沙箱在集群里真跑起来

**未知数清零波**。E2B 在真集群跑不通的话后面全白做,先撞。

- **基建**:沙箱镜像适配(满足 Agent Sandbox 的 runtime 注入要求 —— 已知硬项:**镜像必须含 bash**,W0 PoC 实证;完整清单在波 1 首日按平台文档逐条核对,发现的额外要求补进本节)→ 推 ACR → 配镜像缓存(官方实测 1.34GB 镜像不加速 36s、加速后 4s);`SandboxSet` / `TrafficPolicy` / `SecurityProfile` CR 铺设;**credential-proxy 上集群**(`infra/k8s/base/` 现在没有它,纯加 manifest,不改代码)
- **代码**:Protocol 按 § 4.1 拆分并改名(先只接现有 supervisor 实现,行为零变化);`AgentSandboxClient` 实现 acquire / exec / release / destroy;工厂分支;`sandbox_instance` 热会话 CAS(§ 6.2)
- **工作区**:先不挂 NAS,用 E2B 默认临时盘。**代价**:依赖持久工作区的能力(`read_document` 读历史上传件、产物下载、跨 run 文件保留)在波 1 结束前不可用,波 2 补齐 —— 这是刻意的顺序,不是遗漏
- **验收**:测试环境一个真 agent 跑 `exec_python` 出结果,出网经 credential-proxy 且审计落 `sandbox_egress_audit` 表。**此验收依赖待验证项 2 通过**;若 microVM 到不了集群内 Service,按 § 八的 fallback 走,验收标准同步调整为"经 Ingress 暴露的 credential-proxy"

### 波 2 — 工作区上 NAS

- **基建**:建 NAS 文件系统 + PV;control-plane Pod 挂载;沙箱挂载注解带 subPath
- **代码**:`NasWorkspaceStore`(含路径穿越防护 + 针对性测试,§ 七);§ 4.4 的 8 个调用点改指
- **技能文件搬出工作区**(2026-08-04 追加,拍板保留 per `(tenant, user)` 沙箱语义后的必然结果):
  激活的技能现在 seed 到 `/workspace/skills/<name>/`,而热会话是 per `(tenant, user)` 不含
  agent —— 同一用户先后用两个 agent,两套技能文件会叠在同一个目录里,且后一次 acquire 不清理
  前一次。今天没炸是因为 agent 靠系统提示词知道自己有哪些技能、不扫目录,但这是隐式约定不是保证。
  正解是**技能根本不该放在工作区**:它是 agent 的能力定义,不是用户的数据。写到沙箱本地的
  非持久路径后,沙箱重建即清空、天然不叠加,用户浏览工作区也只看到自己的东西,而且**不占 NAS
  配额**(技能每次 acquire 重 seed,存进按 GiB 计费的持久存储纯浪费)。
  现在做不了是因为 docker 沙箱是 read-only rootfs、`/workspace` 是唯一可写处;microVM 没这个约束。
  放波 2 而不是波 1,是因为波 1 若单改云实现会让两个后端行为分叉,直接打架 Task 10 的契约测试。
- **验收**:上传文档 → agent `read_document` 读到 → agent 写出文件 → 前端下载,全链路真跑;
  技能文件不再出现在用户工作区里

### 波 3 — 会话生命周期与治理

- 热会话映射改 DB + 休眠 / 唤醒语义。**1–10s 唤醒尾延迟要在前端有反馈**:沙箱工具调用进入唤醒等待时发一个状态帧,调试台与对话页把它渲染成"沙箱唤醒中"而不是静默转圈 —— 否则用户会当成卡死
- `reap` 的云实现(运维强制拆除仍要能用)
- 配额:软配额搬新路径 + 后台扫描 job(§ 6.4)
- 归档改从 NAS 目录打包(**杀掉 1.5GiB buffer 上限**);每日全量备份退役 → NAS 快照策略
- 并行查 NAS 原生目录配额(`SetDirQuota`),可行就把软配额换成硬的
- **验收**:沙箱休眠后唤醒继续干活;配额超限被冻结;删用户后归档件在对象存储里躺着

### 波 4 — 收尾

- 死字段裁决:`sandbox_instance.node`、`spec.sandbox` 13 个死字段,逐个决定接线还是删
- 沙箱指标接入 Prometheus + Grafana 面板
- 契约测试补全
- 文档:本地开发怎么跑、发布 runbook、supervisor 冻结声明

**契约测试从波 1 就开始写**,不攒到波 4 —— 它是防两套实现漂移的唯一手段,动手拆分那一刻就得有。

三个判断的理由:

- **波 1 不挂工作区是故意的**。工作区依赖的 NAS 挂载 PoC 已经验过,风险低;E2B 数据面没验过,风险高。先撞高的。
- **credential-proxy 上集群塞在波 1**,因为波 1 的验收要证明出网链路完整。
- **波 3 最容易被低估**。休眠 / 唤醒是语义变化而非接口变化 —— 用户点了下一步、沙箱在唤醒,要等 1–10s。要么 UI 上有反馈,要么被当成卡死。

## 六、关键机制

### 6.1 exec 语义 —— 四个契约点

`infra/sandbox-image/runner.py:45-72` 每次 exec 是 `subprocess.run([sys.executable, "-I", "-c", code])`,**新进程、隔离模式、状态不保持**。"热"的是容器和 `/workspace` 文件,不是 Python 变量。E2B 语义一致,不需要 REPL 模拟。

| 契约 | 现有 | 云实现 |
|---|---|---|
| timeout | clamp `[1, 300]`,缺省 30 | 同,`commands.run(timeout=)` |
| 输出上限 | 1M chars(工具层另有更小的 LLM 预算截断) | 我们自己截,E2B 返回全量 |
| 超时响应 | `exit_code=-1, timed_out=True` | 捕 E2B 超时异常映射成同一 shape |
| 响应形状 | 固定 4 键 `stdout` / `stderr` / `exit_code` / `timed_out` | 同 |

**一个已知偏差**:code 不能直接拼进 shell 命令行(引号注入),得先 `files.write` 到临时文件再 `python -I <file>`。`-c` 模式下 `__file__` 不存在、文件模式下存在。差异极小但真实,**契约测试要钉住**。

`acquire` 的 `seed_files`(agent 激活的技能文件,`(relpath, bytes)` 对)在云实现下走 `sandbox.files.write` 逐个注入,时机在首次 `exec` 之前 —— 与 supervisor 实现的语义一致。

异常类型名 `SandboxSupervisorError` **保留不改**,尽管 Protocol 改名了:它是 `tools` 节点捕获的稳定契约(`ExecPythonTool` 让它传播,ReAct `tools` 节点包成 `ToolMessage(status="error")`),改名会波及错误处理路径而零收益。

### 6.2 热会话与并发 acquire

supervisor 进程内 dict 换成 `sandbox_instance` 表。**复用现有 `container_id` 列**存 E2B sandbox id(同一语义:外部运行时给的实例标识),docstring 写清两种后端各存什么形态 —— 不加新列。

两个 run 同时抢同一个 `(tenant, user)` 的热沙箱时,靠部分唯一索引 + CAS 定单赢家:

```sql
CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
  ON sandbox_instance (tenant_id, user_id)
  WHERE state = 'IN_USE' AND destroyed_at IS NULL;
```

两路都 `INSERT ... ON CONFLICT DO NOTHING RETURNING`:拿到行的建沙箱,没拿到的 `SELECT` 赢家的 `container_id` 直接 connect 上去。与 triggers program 的"端点建唯一行、两路 CAS 同行单赢家"同一配方。

**迁移非 CONCURRENTLY 建索引**:`sandbox_instance` 是低写表(每次 acquire 一行),但仍按仓内惯例进部署 runbook 记录一笔。

### 6.3 休眠 / 唤醒的失败路径

唤醒不是必然成功 —— 库存不足、欠费、保留期已过被平台删,都会失败。**必须有重建路径**:`connect` 失败 → 丢弃该行 → 走 `acquire` 新建。工作区权威在 NAS,所以重建无损。这条路径要有测试,不能只写在注释里。

**休眠保留期定 24h**:休眠态不计 CPU / 内存,但临时存储照收(0.0021 元/GiB/h),per-user 永久保留会累积。过期删沙箱,工作区在外部存储所以无损。24h 是初值,波 3 上线后按实际账单复核,调整只改一个配置项。

`reap(force=True)` 的云实现:E2B SDK 的 `Sandbox.list()` 列出本账号活跃沙箱 → 按 `sandbox_instance` 表里的租户归属过滤 → 逐个 `kill`,同时把行标记 destroyed。运维强制拆除与 M0→M1 Gate E2E 都依赖这个语义,不能只靠平台的保留期到期。

### 6.4 配额

现状:`quota_enforcer.py` 在 acquire 时读 `user_workspace.size_bytes`(release 后 `du` 刷新),是**软限**;真正的天花板是宿主机磁盘 ENOSPC。

**搬到 NAS 后这个天花板消失了** —— 通用型 NAS 容量型按实际写入弹性计费、无容量上限。一个跑飞的 agent 能持续写到 TB 级而无人喊停。

方案:

1. acquire 时的软配额检查照旧(逻辑搬到新路径)
2. **新增周期性扫描 job**:每 10 分钟扫各工作区实际占用,超 `size_limit_bytes` 就标记冻结,下次 acquire 直接拒。最坏损失 = 一个扫描周期内写进去的量。周期是配置项,10 分钟为初值
3. **并行查证 NAS 原生目录配额**(`SetDirQuota`,与 K8s 无关):能用且数量上限够大,就换成硬配额 —— 既硬又不占 K8s 对象、也没有 500 目录的坎;不可行就停在软配额

扫描 job 属于"幂等重复做功类" worker(审计"明确不改"已归类),多副本各跑一遍只是白干,不需要选主。

### 6.5 错误处理与降级

- `AgentSandboxClient` 的所有失败统一抛现有的 `SandboxSupervisorError`,`tools` 节点照常包成 `ToolMessage(status="error")` —— 上游零改
- `sandbox_backend` 没配 → 工厂返 `None` → 沙箱工具不可用,agent 声明了就在构建期失败并给明确错误。现成路径,不动
- 配额冻结、工作区软删仍在 acquire 时拒,复用现有 `WorkspaceQuotaExceededError` / `WorkspaceDeletedError`

## 七、安全面

拆分与迁移带来三处变化,逐条处置:

1. **`NasWorkspaceStore` 的路径穿越**(新增攻击面)。它直接拼文件系统路径,`path` 来自 API 入参。现在这层防护在 supervisor 内,拆出来后必须跟着搬:规范化后校验解析结果仍在 `{root}/{tenant_id}/{user_id}/` 子树内,拒绝符号链接逃逸。**要有针对性测试**(`../`、绝对路径、符号链接、URL 编码变体)。
2. **control-plane Pod 挂 NAS 根**。它要服务所有用户,只能挂整棵树而非 subPath —— 主 API 进程能读所有租户所有用户的工作区文件。不算新增风险(现在经 supervisor 也一样能读全部),但权限半径从"一个专职服务"扩到"主 API 服务",记录在案。
3. **egress 链路不变**:沙箱仍靠 `HTTPS_PROXY=http://<token>:@<proxy>` 指向 credential-proxy,per-sandbox token 认证 + SSRF IP pin + `sandbox_egress_audit` 落表全部照旧。**待验证**:microVM 沙箱能否访问集群内 Service(见 § 八)。

## 八、待验证项与风险

| # | 项 | 落在 | 不通怎么办 |
|---|---|---|---|
| 1 | E2B SDK 数据面经 Ingress 真域名(PoC 里 port-forward 伪装姿势下 502;API 面 / envd / 网络 / 路由表均已验证正常) | 波 1 首日 | 查 gateway 配置;仍不通则退兜底方案(普通 ACS Pod 当沙箱,同 microVM 隔离,损失亚秒创建 / 休眠 / E2B 糖) |
| 2 | microVM 沙箱能否访问集群内 Service(credential-proxy) | 波 1 | TrafficPolicy 放行内网 CIDR;仍不通则 credential-proxy 走 Ingress 暴露(多一跳公网,要加 mTLS) |
| 3 | NAS 原生目录配额 `SetDirQuota` 可行性与数量上限 | 波 3(并行查证) | 停在软配额 + 扫描 job |
| 4 | 沙箱规格档位(1c/1024MB 会被"规整"到未公开档位) | 波 1 | 按平台实际档位调整,更新 `spec.sandbox` 文档 |
| 5 | E2B SDK 版本锁 | 全程 | 钉 `e2b==2.24.0` + `e2b-code-interpreter==2.7.0`(<2.25.0),官方配方 |
| 6 | Agent Sandbox 控制面常驻成本 | 波 1 压测时 | **测试集群实际已按小规格装**(`sandbox-system` 下 `sandbox-gateway` 与 `sandbox-manager` 各 1 副本、各 2c4Gi request=limit,共 4c8Gi,非官方默认的 4c8g×3),量级约 350 元/月按量。风险不在成本而在**吞吐边界未测**:并发创建沙箱时 gateway 是否成瓶颈,波 1 压测看住,不够再往上加 |

## 九、测试策略

**契约测试是这波的核心质量手段**,不是补充。一套用例、两个实现:

```python
@pytest.fixture(params=["supervisor", "agent_sandbox"])
def runtime(request): ...
```

覆盖:§ 6.1 四个契约点 + `__file__` 偏差、唤醒失败重建、并发 acquire 单赢家、配额拒绝、工作区软删拒绝。

三档:

- **单测**:两个实现的纯逻辑(mock 掉 E2B SDK / docker)
- **契约集成**:本地实现真跑 docker;云实现真连测试集群(需凭据,跑在专门的 workflow 里)
- **端到端**:每波验收那条真链路

云实现那半在本地无 ACS 时 skip,但 **CI 里必须有一档真连测试集群跑** —— 否则漂移只会在生产暴露。

## 十、明确不做

- **不做沙箱跨节点调度 / node 感知路由**:平台托管,`node` 概念消失
- **不做 Checkpoint / 沙箱克隆**:E2B 的 checkpoint 仅 filesystem 且与存储挂载互斥,对我们非必需
- **不做 supervisor 的功能增强**:冻结,只维持契约兼容
- **不重写 credential-proxy**:原样搬,只加 k8s manifest
- **不做 per-user PVC 硬配额**:500 目录 / 文件系统的上限对几千用户目标是硬伤

## 十一、与其他工作的关系

- 这波完成后,审计**第 3 波**收口;**第 2 波**(live SSE 跨副本 / 供应商 RPM 全局化)独立,文件面不撞,可并行
- 跨租户 program 的两条上线前必修(quota `commit`/`release` 补 `applied_scope`、迁移 0140 索引进 runbook)随本波的部署波次带走
