# 沙箱迁移波 2 实施计划:工作区上 NAS + 技能搬出工作区

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户工作区权威从沙箱临时盘外置到 NAS(control-plane 直读 + 沙箱领取时动态挂载),技能文件搬出工作区到沙箱临时盘 per-agent 命名空间。

**Architecture:** spec = `docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md`(必读)。两条主线:①`NasWorkspaceStore` 实现既有 `WorkspaceStore` Protocol(control-plane Pod 挂 NAS 整树直读),沙箱经 `e2b.agents.kruise.io/csi-volume-config` metadata 在领取时挂 per-(tenant,user) subPath 到 `/workspace`;②技能 seed 从 `/workspace/skills/<name>/` 改 `/opt/skills/<agent_key>/<name>/`(构建期拼 relpath 前缀,两后端同步,不清理)。

**Tech Stack:** Python 3.12 / FastAPI / e2b==2.24.0(+kruise-agents patch)/ pytest / K8s(ACS)/ NFS(阿里云通用型 NAS 容量型)

## Global Constraints

- E2B SDK 钉 `e2b==2.24.0` + `e2b-code-interpreter==2.7.0`;`patch_e2b(https=False)` 必须在 import e2b 之前(`orchestrator/tools/e2b_patch.py` 已处理,别绕开它)
- 云后端所有 `commands.run` / `files.write` 必须传 `user=SANDBOX_EXEC_USER`(常量,值 `"agent"`)
- 两后端(supervisor / agent_sandbox)行为不得分叉:技能 seed 落点、工作区操作语义、错误类型(`SandboxSupervisorError`)契约测试同断言
- supervisor 冻结例外仅限本波 spec 明文项:tmpfs 挂载 + seed 落点根;其余 supervisor 行为零变化
- 工作区 API 语义 parity 值(supervisor 侧现值,Nas 实现必须同值):读 cap 10MiB(`_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024`)、写 cap 25MiB(`_MAX_WORKSPACE_WRITE_BYTES = 25 * 1024 * 1024`)、list 上限 2000 条(`_MAX_WORKSPACE_LIST_ENTRIES = 2000`)、list 隐藏 + delete 拒绝 `WORKSPACE_RESERVED_PREFIXES`
- 本地跑 integration 测试须 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`
- 提交前跑 CI 同款:`uv run ruff check .`(全库)+ CI-scope mypy(含 tests);orchestrator 测试用 `DOCKER_HOST= uv run pytest`(control-plane 相关经真 run_agent 的测试也要跑)
- kubeconfig = `~/.kube/expert-work-test.yaml`;SandboxSet 在 `default` namespace
- 集群侧凭据只进环境变量不回显;新生成凭据写 600 文件不进对话
- commit 格式 `<type>: <描述>`,无 attribution

## 任务依赖图

```
Task 1(探针,集群实测)──┐
Task 2(k8s manifests)   ├─→ Task 4(云后端挂载+软删闸)─→ Task 7(契约)─→ Task 8(验收文档+发布)
Task 3(NasWorkspaceStore)┘
Task 5(技能 seed 集合+云落点)─→ Task 6(supervisor 技能侧)─→ Task 7
```

Task 3 / Task 5 互不依赖可并行(文件面不撞);Task 1 只产出研究文档与两个待定值(subPath 语义、目录自动建),Task 4 消费。

---

### Task 1: 探针 —— csi-volume-config 配方验证 + ImageCache 实测 + 勘误

**Files:**
- Create: `docs/research/2026-08-07-sandbox-w2-probe-results.md`
- Modify: `docs/research/2026-08-04-sandbox-w1-probe-results.md`(§ 四勘误)

**Interfaces:**
- Produces: 探针结论文档,回答四个问题,Task 4 按答案取值:
  1. `csi-volume-config` 经 `AsyncSandbox.create(metadata=...)` 在**池领取**路径下是否生效
  2. `subPath` 语义:相对 PV `path` 还是相对 NAS 根(决定 Task 4 的 `sandbox_workspace_subpath_prefix` 取值)
  3. 挂载目标目录在 NAS 上不存在时:自动建 or 失败(决定 Task 4 是否要 acquire 前 mkdir——**计划默认按"要 mkdir"写**,探针证明自动建则删掉那一步)
  4. ImageCache 建缓存后补池就绪耗时(基线 35~40s)

前置事实(已核,不用重查):NAS `001qwl4r8snh205ihrs` 运行中;`nas-test-pv`(20Gi RWX)+ `default/nas-test-pvc` 在集群上;sandbox-manager v0.6.8 ≥ 门槛 v0.6.0;`csi-volume-config` 格式(官方文档):

```json
[{"pvName": "nas-test-pv", "mountPath": "/workspace", "subPath": "w2-probe/tenant-a/user-1"}]
```

- [ ] **Step 1: 写探针脚本**(scratchpad,不入仓;形态照 W1 探针)

```python
# probe_csi_mount.py — 跑在能到 gateway 域名的环境(本机可)
# 环境变量:E2B_DOMAIN / E2B_API_KEY(值从 ~/.kube/expert-work-test-secrets.env 读键名后 source,不回显)
import asyncio, json, os
from orchestrator.tools.e2b_patch import ensure_patched  # 若脚本独立跑:先 patch_e2b(https=False) 再 import e2b

async def main() -> None:
    from e2b import AsyncSandbox
    vc = [{"pvName": "nas-test-pv", "mountPath": "/workspace", "subPath": "w2-probe/tenant-a/user-1"}]
    sbx = await AsyncSandbox.create(
        template=os.environ["EXPERT_WORK_SANDBOX_E2B_TEMPLATE"],
        timeout=300,
        metadata={"e2b.agents.kruise.io/csi-volume-config": json.dumps(vc)},
        domain=os.environ["E2B_DOMAIN"],
        api_key=os.environ["E2B_API_KEY"],
    )
    try:
        r = await sbx.commands.run("mount | grep /workspace; id", user="agent")
        print("MOUNT:", r.stdout, r.stderr)
        r = await sbx.commands.run("echo w2-probe-hello > /workspace/probe.txt && cat /workspace/probe.txt", user="agent")
        print("WRITE:", r.stdout, r.exit_code)
    finally:
        await sbx.kill()

asyncio.run(main())
```

- [ ] **Step 2: 跑三种变体,记录结果**
  1. 池领取(默认路径,`kubectl get sbs -n default` 看 AVAILABLE 是否被消耗)——mount 输出应含 NFS 挂载行
  2. subPath 改 `"/w2-probe/tenant-a/user-1"`(带前导 `/`)对照,判语义
  3. subPath 指向 NAS 上确认不存在的新目录,判自动建
  验证跨 Pod 共享:`kubectl run nas-check --rm -it --image=busybox --overrides=...`(挂 `nas-test-pvc`)读 `probe.txt` 内容一致。

- [ ] **Step 3: ImageCache 实测**
  控制台(用户点或一起看):容器计算服务 → 镜像缓存 → 创建,镜像选 ACR `expert-work/sandbox:<当前 SandboxSet tag>`,VPC 内网,同账号 ACR 免密。制作完成后:`kubectl delete sbx <池内沙箱名> -n default` 触发补池,计时到 `kubectl get sbs` AVAILABLE 回 1;对照 Pod 注解 `image.alibabacloud.com/matched-image-caches` 是否出现。记录耗时。

- [ ] **Step 4: 写探针结果文档**
  `docs/research/2026-08-07-sandbox-w2-probe-results.md`:四问四答 + 原始输出摘录 + 对 Task 4 两个取值的明确指示。清理:删 `w2-probe/` 探针残渣与 NAS 根上的 PoC 残渣(`/tenant-a` 等,经 busybox pod);**保留** `nas-test-pv`/`nas-test-pvc`(契约测试还要用)。

- [ ] **Step 5: 勘误 W1 文档 + commit**
  `2026-08-04-sandbox-w1-probe-results.md` § 四:删"邀测需白名单/工单"表述,改为"ACS 镜像缓存无需开通,单地域默认配额 200,控制台/OpenAPI 创建;沙箱池扩容默认吃缓存(`ops.alibabacloud.com/update-with-image-cache` 默认 false)",引现行文档。表格待办第 5 行同步改。

```bash
git add docs/research/
git commit -m "docs: 沙箱 W2 探针结果——csi-volume-config 池领取挂载实测 + ImageCache 勘误"
```

---

### Task 2: K8s manifests —— workspace-nas PV/PVC + control-plane 挂载

**Files:**
- Create: `infra/k8s/base/control-plane/workspace-nas.yaml`(PV + PVC)
- Modify: `infra/k8s/base/control-plane/deployment.yaml`(volumeMounts/volumes)
- Modify: `infra/k8s/base/kustomization.yaml`(resources 补新文件)
- Modify: `infra/k8s/overlays/test/configmap-patch.yaml`(新 env)

**Interfaces:**
- Produces: PV 名 `workspace-nas`(Task 4 的 `sandbox_workspace_pv_name` 默认引用);容器内挂载点 `/mnt/workspaces`(`workspace_nas_root` 的 overlay 值)
- 环境变量名(pydantic-settings 前缀规则):`EXPERT_WORK_WORKSPACE_NAS_ROOT` / `EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME` / `EXPERT_WORK_SANDBOX_WORKSPACE_SUBPATH_PREFIX`

- [ ] **Step 1: 写 PV/PVC**

```yaml
# infra/k8s/base/control-plane/workspace-nas.yaml
# 沙箱迁移波 2 —— 用户工作区权威存储(NAS)。
# PV 双消费:control-plane 经 PVC 挂整树;沙箱经 csi-volume-config 引 pvName
# 挂 per-(tenant,user) subPath(W0 PoC 实证 Bound 态 PV 可被沙箱同时引用)。
apiVersion: v1
kind: PersistentVolume
metadata:
  name: workspace-nas
spec:
  capacity:
    storage: 500Gi          # NAS 容量型按实际写入计费,此值仅调度用
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: nasplugin.csi.alibabacloud.com
    volumeHandle: workspace-nas
    volumeAttributes:
      server: <NAS 挂载点域名>      # 实施时从控制台/nas-test-pv 的 server 字段抄,格式 001qwl4r8snh205ihrs-xxxx.cn-hangzhou.nas.aliyuncs.com
      path: /workspaces
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: workspace-nas
  namespace: expert-work
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: workspace-nas
  resources:
    requests:
      storage: 500Gi
```

`server` 值实施时查:`kubectl get pv nas-test-pv -o jsonpath='{.spec.csi.volumeAttributes}'`。

- [ ] **Step 2: control-plane Deployment 加挂载**

`deployment.yaml` 的 `volumeMounts:`(35 行附近)加:

```yaml
            - name: workspace-nas
              mountPath: /mnt/workspaces
```

`volumes:`(78 行附近)加:

```yaml
        - name: workspace-nas
          persistentVolumeClaim:
            claimName: workspace-nas
```

- [ ] **Step 3: overlay env**

`infra/k8s/overlays/test/configmap-patch.yaml` 沙箱配置节(`EXPERT_WORK_SANDBOX_E2B_TEMPLATE` 附近)加:

```yaml
  EXPERT_WORK_WORKSPACE_NAS_ROOT: "/mnt/workspaces"
  EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME: "workspace-nas"
  EXPERT_WORK_SANDBOX_WORKSPACE_SUBPATH_PREFIX: ""   # 探针结论若为"相对 NAS 根"则改 "workspaces"
```

- [ ] **Step 4: 渲染验证**

```bash
kustomize build infra/k8s/overlays/test > /tmp/w2-render.yaml
grep -n "workspace-nas\|/mnt/workspaces\|WORKSPACE_NAS" /tmp/w2-render.yaml
```

预期:PV/PVC/volumeMount/env 全出现;`kubectl apply --dry-run=client -f /tmp/w2-render.yaml` 无错。
NAS 上 `/workspaces` 目录:发布 runbook 步骤(经 busybox pod 挂 `nas-test-pvc` `mkdir -p /mnt/workspaces`——nas-test-pv 的 path 指 NAS 根),写进 Task 8 的发布节。

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/
git commit -m "feat(k8s): workspace-nas PV/PVC + control-plane NAS 挂载(沙箱迁移波 2)"
```

---

### Task 3: NasWorkspaceStore + 工厂 + 设置

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py`
- Test: `services/orchestrator/tests/test_nas_workspace_store.py`
- Modify: `services/control-plane/src/control_plane/settings.py`(3 个新字段,`sandbox_e2b_template` 之后)
- Modify: `services/control-plane/src/control_plane/runtime.py:1516`(`build_workspace_store` 签名)
- Modify: `services/control-plane/src/control_plane/app.py:717`(调用点)

**Interfaces:**
- Consumes: `WorkspaceStore` Protocol / `WorkspaceFileEntry`(`orchestrator/tools/workspace_store.py:41-80`,原样);`SandboxSupervisorError`(`orchestrator/tools/sandbox.py`);`is_reserved_workspace_path`(`expert_work.persistence`)
- Produces:

```python
@dataclass
class NasWorkspaceStore:
    root: str                      # control-plane Pod 内挂载点,如 /mnt/workspaces
    runtime: SandboxRuntime | None = None   # Task 4 接线;本 task 恒 None
    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes: ...
    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]: ...
    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None: ...
    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None: ...
    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None: ...

DELETED_MARKER = ".ew-workspace-deleted"   # 模块级常量,Task 4 & 7 引用
def build_workspace_store(settings: Settings) -> WorkspaceStore | None  # runtime.py,签名从 (url) 改 (settings)
```

- 设置新字段(settings.py,含 docstring):

```python
    #: 波 2 —— NAS 工作区根(control-plane Pod 内挂载点)。设了即选
    #: ``NasWorkspaceStore``(优先于 supervisor 代理);None → 按老路径。
    workspace_nas_root: str | None = None
    #: 波 2 —— 沙箱挂工作区用的 PV 名(csi-volume-config 的 pvName)。
    sandbox_workspace_pv_name: str | None = None
    #: 波 2 —— csi-volume-config subPath 前缀(探针定语义;"" = 相对 PV path)。
    sandbox_workspace_subpath_prefix: str = ""
```

**语义 parity 清单**(supervisor 侧 `supervisor.py:485-583` 为准,契约同断言):

| 方法 | 行为 |
|---|---|
| `read_file` | 路径校验(相对、无 `..`);>10MiB 抛 `SandboxSupervisorError`;不存在抛 `SandboxSupervisorError` |
| `list_files` | 递归 `(size, relpath)`;隐藏 `is_reserved_workspace_path` 前缀 + `DELETED_MARKER`;上限 2000 条;用户目录不存在 → `[]` |
| `write_file` | >25MiB 拒;`mkdir parents`;路径校验同上 |
| `delete_file` | reserved 前缀拒;缺文件 no-op(`rm -f` 语义) |
| `mark_deleted` | 用户目录写 `DELETED_MARKER` 空文件(目录不存在先建);幂等;文件保留(归档链波 3) |

错误类型统一:所有失败抛 `SandboxSupervisorError`(与 `SupervisorWorkspaceStore` 把非 2xx 包成它同构——上层 8 个调用点只认这一个类型)。

**路径穿越防护**(核心,先写测试):

```python
def _resolve_user_path(self, tenant_id: UUID, user_id: UUID, path: str) -> Path:
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        raise SandboxSupervisorError(f"workspace path must be relative and free of '..': {path!r}")
    user_root = (Path(self.root) / str(tenant_id) / str(user_id)).resolve()
    candidate = (user_root / cleaned).resolve()   # resolve 展开符号链接
    if not candidate.is_relative_to(user_root):
        raise SandboxSupervisorError(f"workspace path escapes the user root: {path!r}")
    return candidate
```

- [ ] **Step 1: 写失败测试**(`tmp_path` 真文件系统,零 mock)——最少覆盖:
  - 穿越四件套:`../x`、`/etc/passwd`、符号链接指向 root 外(真建 symlink 后读它必须拒)、URL 编码变体 `%2e%2e%2f`(**字面处理**——store 不做 URL 解码,断言该字符串被当普通文件名、不逃逸)
  - 读写删列 roundtrip;list 隐藏 `skills/`、`uploads/`、marker;delete 拒 `uploads/a.txt`;delete 缺文件 no-op
  - 读 cap:写 10MiB+1 字节文件(seek 稀疏),读抛;写 cap:25MiB+1 拒
  - `mark_deleted` 幂等 + marker 落盘 + list 不含 marker
  - list 用户目录不存在 → `[]`
- [ ] **Step 2: 跑测试确认全 FAIL**(模块不存在)
- [ ] **Step 3: 实现 `NasWorkspaceStore`**(文件 I/O 用 `asyncio.to_thread` 包——NFS 上的同步 I/O 不能卡事件循环)
- [ ] **Step 4: 跑测试 PASS**
- [ ] **Step 5: 工厂 + 设置 + 接线**:settings 3 字段;`build_workspace_store(settings)`——`workspace_nas_root` 真值 → Nas;elif `sandbox_supervisor_url` 真值 → Supervisor;else None。app.py:717 改传 settings。修既有工厂测试(`not url` 判据那批,test #77 提过的)。
- [ ] **Step 6: 全量验证 + commit**

```bash
cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_nas_workspace_store.py -v
cd ../control-plane && DOCKER_HOST= uv run pytest tests/ -k "workspace_store or build_workspace" -v
uv run ruff check . && <CI-scope mypy>
git add -A && git commit -m "feat(sandbox): NasWorkspaceStore——NAS 直读工作区 + 工厂按 workspace_nas_root 选型"
```

---

### Task 4: 云后端挂载注入 + 软删闸 + mark_deleted 热会话拆除

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(`_create`、`acquire`、dataclass 字段)
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox_instance_store.py`(Protocol + 两实现加 `get_warm`)
- Modify: `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py`(`mark_deleted` 热会话拆除)
- Modify: `services/control-plane/src/control_plane/runtime.py`(`build_sandbox_runtime` 传新字段;`build_workspace_store` 接 runtime)
- Test: `services/orchestrator/tests/test_agent_sandbox.py`(扩)、`tests/test_nas_workspace_store.py`(扩)

**Interfaces:**
- Consumes: Task 1 结论(subPath 前缀值、目录是否自动建)、Task 3 的 `NasWorkspaceStore` / `DELETED_MARKER`
- Produces:

```python
# AgentSandboxClient 新字段(全部默认 None/"",不配 = 行为与波 1 完全一致):
workspace_pv_name: str | None = None
workspace_subpath_prefix: str = ""
workspace_root: str | None = None    # control-plane 本地挂载点,mkdir + 软删闸用

# SandboxInstanceStore Protocol 新方法(SQL + InMemory 两实现):
async def get_warm(self, *, tenant_id: UUID, user_id: UUID) -> tuple[UUID, str] | None:
    """该用户活跃热会话的 (sandbox_id, container_id);无则 None。"""
```

- 挂载注入(`_create`,仅 `user_id is not None` 且 `workspace_pv_name` 配了时):

```python
subpath = "/".join(p for p in (self.workspace_subpath_prefix, str(tenant_id), str(user_id)) if p)
metadata = {
    "e2b.agents.kruise.io/csi-volume-config": json.dumps(
        [{"pvName": self.workspace_pv_name, "mountPath": WORKSPACE_ROOT, "subPath": subpath}]
    )
}
```

  `_create` 签名加 `user_id: UUID | None`(acquire 传入;`create_ephemeral` 路径传 None → 不挂,与 supervisor "临时沙箱 tmpfs /workspace" 对齐)。
- acquire 软删闸(`claim_warm` 之前):`workspace_root` 配了且 `{root}/{tenant}/{user}/.ew-workspace-deleted` 存在 → 抛 `SandboxSupervisorError("workspace deleted for user ...")`(supervisor 对软删工作区同样在 acquire 拒,HTTP 客户端同样包成 `SandboxSupervisorError`——工具层可观察契约一致)
- acquire 前 mkdir(探针说"不自动建"才保留):`workspace_root` 配了 → `Path(root, str(tenant), str(user)).mkdir(parents=True, exist_ok=True)`(`asyncio.to_thread`)
- `NasWorkspaceStore.mark_deleted` 补:`runtime` 非 None 时,`store.get_warm` 查热会话 → `runtime.destroy(sandbox_id=..., reason="workspace_deleted")`(destroy 已有标记行 + kill 语义);查/杀失败不吞——marker 先落盘,拆除失败抛错让 purge 记 failure(marker 已挡住后续 acquire,最终一致)
- 接线顺序(app.py):先 `build_sandbox_runtime` 再 `build_workspace_store(settings, runtime=..., instance_store=...)`

- [ ] **Step 1: 写失败测试**
  - `test_create_injects_csi_volume_config`:FakeSDK 捕 `create(metadata=...)`,断言 JSON 三键与 subPath 拼接(含 prefix 空/非空两例)
  - `test_ephemeral_create_has_no_volume_config`(user_id=None)
  - `test_acquire_refuses_deleted_workspace`(tmp_path 造 marker → acquire 抛)
  - `test_acquire_mkdirs_user_workspace`(探针若判自动建则删本条与实现)
  - `test_get_warm_*`:InMemory + SQL(SQL 侧进现有 store 集成测试文件,`DOCKER_HOST` 真容器跑——**SQL 变异复验 file-scope**)
  - `test_mark_deleted_destroys_warm_session`(FakeRuntime 记录 destroy 调用)
- [ ] **Step 2: 跑全 FAIL** → **Step 3: 实现** → **Step 4: 跑 PASS**
- [ ] **Step 5: runtime.py 接线 + 既有工厂测试修**;`DOCKER_HOST= uv run pytest tests/ -k "agent_sandbox or nas_workspace or instance_store"` 全绿
- [ ] **Step 6: Commit** `feat(sandbox): 云后端领取时挂 NAS 工作区 + 软删闸 + purge 热会话拆除`

---

### Task 5: 技能 seed —— per-agent 命名空间 + 云落点

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/layout.py`(+`SANDBOX_SKILLS_ROOT`)及包 `__init__` 两级导出
- Modify: `services/orchestrator/src/orchestrator/tools/skill_seed.py`(`build_skill_seed_files` 加 `agent_key`,anchor 改)
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py:708-715`(传 agent_key + 注释)
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py:479`(seed 根 `WORKSPACE_ROOT` → `SANDBOX_SKILLS_ROOT`)
- Modify: 路径文案:`tools/sandbox.py:543`、`tools/bash.py:82`、`tools/file_ops.py:471/528`、`tools/read_document.py:184`、`tools/assembly.py`(grep `workspace/skills` 全改)
- Test: `services/orchestrator/tests/test_skill_seed.py`、`tests/test_agent_sandbox.py:591`、`tests/test_exec_python_tool.py:65`
- Modify: `tools/eval/verify_live_skill_runtime.py`(路径)

**Interfaces:**
- Produces:

```python
# layout.py
SANDBOX_SKILLS_ROOT = "/opt/skills"   # 两后端 seed 落点根(supervisor Task 6 同源引用)

# skill_seed.py
def sanitize_agent_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", name) or "agent"

async def build_skill_seed_files(
    resolved_versions, activated_skill_names, *, agent_key: str, object_store=None
) -> SkillSeedResult:
    # anchor: f"{agent_key}/{name}/SKILL.md" / f"{agent_key}/{name}/{relpath}"
    # (WORKSPACE_SKILLS_DIR 不再是 anchor;常量与 reserved 过滤保留——老工作区残渣仍要隐藏)
```

- `agent_factory` 调用点:`agent_key=sanitize_agent_key(spec.metadata.name)`;注释同步(materialized under `/opt/skills/<agent_key>/<name>/`)
- 系统提示词:凡向 agent 陈述技能文件位置处,插值具体路径 `/opt/skills/{agent_key}/<skill>/`(grep `workspace/skills` 定位全部陈述点,一处不留)
- **不做清理**(spec 决策 4):并发安全靠命名空间;重复 seed 幂等

- [ ] **Step 1: 写失败测试**:`test_seed_paths_are_agent_namespaced`(relpath 前缀 = sanitized key)、`test_sanitize_agent_key`(空名/中文/斜杠)、`test_agent_sandbox` 591 行断言改 `("/opt/skills/<key>/…", …)`
- [ ] **Step 2: FAIL** → **Step 3: 实现(layout 常量 + skill_seed + agent_factory + agent_sandbox.py:479 + 文案)** → **Step 4: PASS**
- [ ] **Step 5: 全库 grep 复核**:`rg -n "workspace/skills" --type py` 只剩历史文档;`DOCKER_HOST= uv run pytest`(orchestrator 全量)
- [ ] **Step 6: Commit** `feat(skills): 技能 seed 搬 /opt/skills/<agent_key>/——per-agent 命名空间,退出用户工作区`

---

### Task 6: supervisor 技能侧 —— tmpfs 挂载 + seed 落点根

**Files:**
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/docker_client.py`(run 参数 + `seed_workspace` 的 `-C` 目标)
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/supervisor.py:277-317`(`_seed_workspace` docstring/路径校验不变,落点随 docker 层)
- Test: `services/sandbox-supervisor/tests/`(run-args 断言测试 + seed 落点测试,既有文件内扩)

**Interfaces:**
- Consumes: `SANDBOX_SKILLS_ROOT`(Task 5,经 `expert_work.persistence` 导入——supervisor 已依赖该包)
- Produces: 沙箱容器多一块挂载 `--tmpfs /opt/skills:rw,size=64m,uid=10000,gid=10000`;seed tar 解到 `-C /opt/skills`

要点:
- `docker run` 参数在 `docker_client.py` 各 run 组装处(224/290/347/390/433/473 行族,`--read-only` 邻位)统一加 tmpfs 行——抽一个共享常量列表,别六处手拼
- `seed_workspace` 用 `docker exec <container> tar -xf - -C /opt/skills`(现机制对 tmpfs 已被 ephemeral `/workspace` 证明,零兼容风险)
- supervisor 的 `_validate_workspace_path`(相对、无 `..`)继续复用——relpath 现在带 `<agent_key>/` 前缀,仍是合法相对路径,校验零改
- 冻结例外仅此两处,别顺手改任何其他行为

- [ ] **Step 1: 写失败测试**(run-args 含 tmpfs;seed 后 `docker exec cat /opt/skills/<key>/a/SKILL.md` 回读——integration 档带 `DOCKER_HOST`)
- [ ] **Step 2: FAIL** → **Step 3: 实现** → **Step 4: PASS**(supervisor 全量测试跑一遍,3483 行套件是回归网)
- [ ] **Step 5: Commit** `feat(supervisor): /opt/skills tmpfs + 技能 seed 落点迁移(波 2 两后端同步)`

---

### Task 7: 契约测试 —— WorkspaceStore 两实现 + 技能落点 + e2b 挂载档

**Files:**
- Create: `services/orchestrator/tests/test_workspace_store_contract.py`
- Modify: `services/orchestrator/tests/test_sandbox_runtime_contract.py`(技能 seed 落点断言 + e2b 挂载档)
- Modify: `.github/workflows/sandbox-contract.yml`(新 env:`EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME=nas-test-pv`)

**Interfaces:**
- Consumes: Task 3/4/5/6 全部产物
- 跑法照 `test_sandbox_runtime_contract.py` 现骨架:env 未设 skip 对应参数,不失败

- [ ] **Step 1: WorkspaceStore 契约套件**——`@pytest.fixture(params=["supervisor", "nas"])`;nas 档 `tmp_path` 即建;supervisor 档 `EXPERT_WORK_SANDBOX_SUPERVISOR_URL` 未设 skip。用例:roundtrip、list 隐藏 reserved、delete 拒 reserved、读写 cap 同值、`mark_deleted` 后行为、错误类型恒 `SandboxSupervisorError`
- [ ] **Step 2: 技能落点契约**——两 runtime 后端 seed 后,exec `cat /opt/skills/<key>/<skill>/SKILL.md` 回读一致(supervisor 档真 docker;e2b 档真集群)
- [ ] **Step 3: e2b 挂载档**——`EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME` 未设 skip;设了:acquire(带 user)→ exec 写 `/workspace/contract-probe.txt` → **第二个沙箱**同 (tenant,user) acquire → exec 读到同内容(跨沙箱共享证权威在 NAS,不依赖 CI runner 挂 NFS)→ 双沙箱 destroy + NAS 清理
- [ ] **Step 4: workflow env 补齐**;本地能跑的档全绿;`ruff` + mypy
- [ ] **Step 5: Commit** `test(sandbox): WorkspaceStore 契约两实现 + 技能落点 + e2b NAS 挂载档`

---

### Task 8: 文档 + 发布步骤 + 端到端验收清单

**Files:**
- Modify: `docs/runbooks/sandbox-image-release.md` 或新增发布节(波 2 首发步骤)
- Modify: `docs/design/skill-runtime-capability.md`(135/301 行路径陈述)
- Create: 无新文档;验收记录追加进 Task 1 的 probe-results 文档

- [ ] **Step 1: 发布步骤写进 runbook**(顺序敏感):
  1. busybox pod 挂 `nas-test-pvc`:`mkdir -p /mnt/nas/workspaces`
  2. `kubectl apply` PV/PVC(base 渲染的 workspace-nas)
  3. release.sh 常规发布(新镜像含全部代码;deployment 带挂载)
  4. 冒烟:`kubectl exec deploy/control-plane -- ls /mnt/workspaces`
- [ ] **Step 2: 设计文档路径陈述更新** + spec/probe 文档交叉链接补全
- [ ] **Step 3: 端到端验收清单**(发布测试环境后与用户真栈跑;吸收 W1 Task 11):

```
□ 前端上传文档 → NAS 上 {tenant}/{user}/uploads/ 出现
□ agent read_document 读到内容
□ agent exec_python 写 /workspace/out.txt → 前端工作区浏览可见 + 下载内容一致
□ 工作区浏览不含 skills/、uploads/(reserved 隐藏)且 NAS 用户目录下无技能文件
□ kubectl delete sbx <该用户沙箱> → 再跑 agent → out.txt 仍在(权威在 NAS)
□ 沙箱内 cat /opt/skills/<agent_key>/<skill>/SKILL.md 有内容
□ (W1 Task 11)exec 出网经 credential-proxy,sandbox_egress_audit 表落行
□ 删用户 → purge 成功,NAS 目录留 marker,acquire 被拒
```

- [ ] **Step 4: Commit** `docs: 波 2 发布步骤 + 端到端验收清单`

---

## Self-Review 结论(计划作者自查)

- spec § 一~§ 十 全部映射:官方新事实→Task 1;布局/基建→Task 2;NasWorkspaceStore/穿越→Task 3;挂载注入/软删→Task 4;技能 A′→Task 5/6;契约→Task 7;验收/发布→Task 8;勘误→Task 1。
- 类型一致性:`build_workspace_store(settings)` 签名 Task 3 定义、Task 4 扩参;`SANDBOX_SKILLS_ROOT`/`DELETED_MARKER`/`get_warm` 的定义与消费任务一一对应。
- 两个探针待定值(subPath 前缀、mkdir 是否需要)在 Task 2/4 都写了默认按哪边执行、另一边怎么删——不是留白。
