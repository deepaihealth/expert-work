# 沙箱迁移波 1「沙箱在集群里真跑起来」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 测试集群里一个真 agent 能跑通 `exec_python`,出网经 credential-proxy 且审计落 `sandbox_egress_audit` 表。

**Architecture:** 现有 `SupervisorClient` Protocol 一拆二 —— `SandboxRuntime`(沙箱生命周期与执行,5 方法)+ `WorkspaceStore`(工作区文件,5 方法)。各两个实现,单一配置项 `sandbox_backend` 选:本地/CI 走现有 docker supervisor,云上走新写的 `AgentSandboxClient`(E2B SDK)。本波工作区仍用 E2B 临时盘,NAS 在波 2 接。

**Tech Stack:** Python 3.12 / asyncio、E2B SDK、SQLAlchemy + Alembic、pytest、Kubernetes(ACS)+ kustomize、阿里云 ACR

## Global Constraints

以下取值逐字来自 spec `docs/superpowers/specs/2026-08-03-sandbox-migration-design.md`,每个任务的要求都隐含包含本节。

- **E2B SDK 版本钉死**:`e2b==2.24.0` + `e2b-code-interpreter==2.7.0`(必须 `<2.25.0`,官方配方)
- **沙箱镜像必须含 bash**(Agent Sandbox 的 runtime 注入钩子依赖;W0 PoC 实证)
- **异常类型名 `SandboxSupervisorError` 保留不改**,尽管 Protocol 改名 —— 它是 `tools` 节点捕获的稳定契约
- **`sandbox_instance` 不加新列**:复用现有 `container_id` 列存 E2B sandbox id(同一语义:外部运行时给的实例标识)
- **exec 四个契约点**:timeout clamp `[1, 300]` 缺省 30;输出上限 1_000_000 chars;超时响应 `exit_code=-1, timed_out=True`;响应固定 4 键 `stdout`/`stderr`/`exit_code`/`timed_out`
- **本波不挂 NAS**,工作区用 E2B 默认临时盘;依赖持久工作区的能力(`read_document` 读历史上传件、产物下载、跨 run 文件保留)本波不可用,波 2 补齐
- **supervisor 冻结**:不加新功能,只维持契约兼容
- **仓内文件规模**:单文件 800 行上限(`sandbox.py` 现 799 行,拆分是必要而非顺手重构)
- 迁移非 CONCURRENTLY 建索引按仓内惯例,进部署 runbook 记一笔
- 本地跑 integration 测试须 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`
- 集群操作须 `export KUBECONFIG=~/.kube/expert-work-test.yaml`

---

## 文件结构

### 新建

| 文件 | 职责 |
|---|---|
| `services/orchestrator/src/orchestrator/tools/workspace_store.py` | `WorkspaceStore` Protocol + `SupervisorWorkspaceStore`(HTTP 转 supervisor)+ `RecordingWorkspaceStore`(测试假件) |
| `services/orchestrator/src/orchestrator/tools/agent_sandbox.py` | `AgentSandboxClient` —— E2B SDK 实现的 `SandboxRuntime` |
| `services/orchestrator/tests/test_agent_sandbox.py` | `AgentSandboxClient` 单测(mock SDK) |
| `services/orchestrator/tests/test_sandbox_runtime_contract.py` | 契约测试:一套用例两个实现 |
| `services/orchestrator/tests/test_workspace_store.py` | `WorkspaceStore` 两实现单测 |
| `packages/expert-work-persistence/migrations/versions/0141_sandbox_warm_unique.py` | 热会话部分唯一索引 |
| `infra/k8s/base/credential-proxy/` | credential-proxy 的 Deployment + Service + kustomization |
| `infra/k8s/base/sandbox/` | `SandboxSet` / `TrafficPolicy` / `SecurityProfile` CR |

### 修改

| 文件 | 改什么 |
|---|---|
| `services/orchestrator/src/orchestrator/tools/sandbox.py` | 删 5 个 workspace 方法(搬走);`SupervisorClient` → `SandboxRuntime`;`RecordingSupervisorClient` → `RecordingSandboxRuntime` |
| `services/control-plane/src/control_plane/runtime.py:1448` | `build_supervisor_client` → `build_sandbox_runtime` + `build_workspace_store`,按 `sandbox_backend` 分支 |
| `services/control-plane/src/control_plane/settings.py:202` | 加 `sandbox_backend` + E2B 接入所需配置 |
| `services/control-plane/src/control_plane/app.py:695,1300` | 注入点改指两个新工厂 |
| `services/control-plane/src/control_plane/api/{workspace,sessions,artifacts,uploads,sandboxes}.py`、`purge/user_purge.py` | 8 个工作区调用点改指 `workspace_store` |
| `services/orchestrator/pyproject.toml` | 加 E2B SDK 依赖 |
| `infra/sandbox-image/Dockerfile` | 确保含 bash + Agent Sandbox runtime 注入要求 |
| `infra/k8s/base/kustomization.yaml` | 挂上 credential-proxy 与 sandbox 两个新目录 |
| `infra/k8s/overlays/test/` | 环境值(镜像 tag、egress token secret、E2B 接入点) |

### 任务依赖与并行

```
Task 1 (镜像) ──> Task 2 (CR + proxy 上集群) ──> Task 3 (风险探针) ──┐
                                                                      ├──> Task 7,8,9 ──> Task 10 ──> Task 11
Task 4 (拆 WorkspaceStore) ──> Task 5 (改名) ──> Task 6 (配置+工厂) ──┘
```

**Task 1-3 与 Task 4-6 两条链互不依赖,可并行 worktree**。Task 3 是**门**:三个未验证项不通就停下来跟人讨论退兜底,不要硬往下做。

---

## Task 1: 沙箱镜像适配 + 推 ACR

**Files:**
- Modify: `infra/sandbox-image/Dockerfile`
- Modify: `docs/superpowers/plans/2026-08-03-sandbox-migration-w1.md`(把首日核对出的平台要求补回本任务)

**Interfaces:**
- Produces: ACR 上的镜像 `crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:<sha>`,Task 2 的 `SandboxSet` CR 引用它

- [ ] **Step 1: 核对镜像当前是否含 bash**

```bash
cd /Users/mac/src/github/jone_qian/expert-work
grep -n "bash\|FROM\|apt-get\|apk" infra/sandbox-image/Dockerfile
```

已知硬要求:**镜像必须含 bash**(W0 PoC 实证 —— Agent Sandbox 的 runtime 注入钩子依赖 bash)。若基础镜像是 slim/alpine 且未装 bash,下一步补。

- [ ] **Step 2: 逐条核对 Agent Sandbox 的镜像要求**

打开阿里云 Agent Sandbox 自定义镜像文档,对照检查(bash 之外还有什么强制项,例如 agent-runtime/envd 的注入方式、必须存在的目录、entrypoint 约束)。**把核对出的每一条追加写进本任务的 Step 3**,不要只记在脑子里 —— 后面三波都要照这份清单。

**核对结果(2026-08-03)** —— 环境内无可用浏览器/WebFetch 工具,核对经 WebSearch 摘要交叉验证(同一措辞在 3 次独立查询、中英文页面中一致复现),置信度高但非逐字读取源文档;完整过程见 `.superpowers/sdd/2026-08-03-sandbox-migration-w1/task-1-report.md`:

| # | 要求 | 强制性 | 本镜像现状 | 动作 |
|---|---|---|---|---|
| 1 | bash 必须存在,可执行文件位于 `/bin/bash`(来源:help.aliyun.com/zh/cs/user-guide/create-an-agent-sandbox) | 硬性,文档原文 | 已满足 —— Debian `Essential:yes` 包,`python:3.12-slim` 自带;amd64/arm64 双架构实测 `dpkg -l` 命中 `bash 5.2.37-2+b9` | Dockerfile 加 build-time assertion(校验,非安装) |
| 2 | 基础指令 cp/mv/mkdir"等"(非穷举列表) | 硬性,文档原文 | 已满足 —— `coreutils 9.7-3`,同为 Debian Essential 包 | 同一条 assertion 一并校验 |
| 3 | 自定义镜像下 E2B `run_code()` 方法不可用,只剩 `commands.run`(envd shell-exec 通道)可用(来源:同上 + connect-to-agent-sandbox-using-the-e2b-sdk) | 限制/后果,非镜像改动项 | 不影响 —— spec § 6.1 本就规划走 `commands.run`,未依赖 `run_code` | 记录给 Task 8:不能假设 `run_code` 可用 |
| 4 | agent-runtime/envd 经 Kubernetes native sidecar 注入(`SandboxSet.spec.runtimes: [agent-runtime]`,sidecar 镜像 `registry-*.ack.aliyuncs.com/acs/agent-runtime:<ver>`,经共享卷挂到 `$ENVD_DIR`,`__IGNORE_RESOURCE__=true` 免占资源配额) | 平台侧机制,非镜像要求 | 不需要 Dockerfile 改动 —— 由 SandboxSet CR 声明 | Task 2 在 CR 里配 `spec.runtimes` |
| 5 | 若要用 ACS 官方 `run_code`/code-interpreter 镜像,须直接用或以 ACS 版本化镜像为基础,不保证兼容 E2B 官方 latest 镜像 | 不适用 | 我们全程走自定义全量镜像 + `commands.run`,不碰这条路径 | 无动作 |
| 6 | 镜像缓存是独立 CRD(`apiVersion: eci.alibabacloud.com/v1, kind: ImageCache`,复用 ECI 同款 CRD),官方文档称目前邀测阶段、白名单/工单开通 | 待确认账号状态 | 见 Step 7 实测结论 | 见 Step 7 |
| 7 | 自定义镜像 root / 非 root 用户要求 | **未找到明确要求** | 本镜像已是 `USER agent`(uid 10000,非 root)—— 未发现与此冲突的官方说明 | 无动作;若后续 Task 2/3 探针发现 envd 注入对非 root 有特殊要求,回来补 |
| 8 | 支持的 CPU 架构(是否仅 amd64) | **未找到明确要求**(未查到架构矩阵文档) | 全局约束已强制 `--platform linux/amd64`,与已知事实(Apple Silicon 本机 push 会走样)独立自洽,不依赖这条未证实信息 | 无动作 |

- [ ] **Step 3: 按核对结果改 Dockerfile**

至少确保 bash 存在。示例(基础镜像为 Debian 系时):

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash \
    && rm -rf /var/lib/apt/lists/*
```

Alpine 系用 `apk add --no-cache bash`。Step 2 核对出的其它要求一并加在这里。

- [ ] **Step 4: 本地构建并验证 bash 在**

```bash
docker build --platform linux/amd64 -f infra/sandbox-image/Dockerfile -t expert-work-sandbox:w1-check infra/sandbox-image
docker run --rm --platform linux/amd64 expert-work-sandbox:w1-check bash -lc 'echo BASH_OK && python -c "print(1+1)"'
```

Expected: 输出 `BASH_OK` 和 `2`。

- [ ] **Step 5: 跑既有沙箱镜像烟测**

```bash
docker run --rm --platform linux/amd64 expert-work-sandbox:w1-check python /opt/expert-work/smoke_test.py
```

Expected: 退出码 0。(路径以 `infra/sandbox-image/Dockerfile` 里 `smoke_test.py` 的实际落点为准,构建前先 `grep -n smoke_test infra/sandbox-image/Dockerfile` 确认。)

- [ ] **Step 6: 推 ACR**

```bash
TAG=$(git rev-parse --short=8 HEAD)
REG=crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work
docker tag expert-work-sandbox:w1-check "$REG/sandbox:$TAG"
docker push "$REG/sandbox:$TAG"
```

**必须 `--platform linux/amd64`** —— containerd 存储的 push 会挑宿主机架构,Apple Silicon 上不显式指定会推出 arm64 镜像,集群上 exec format error(W2-PR3 踩过)。

- [ ] **Step 7: 配镜像缓存**

按 ACS 镜像缓存文档给这个镜像建缓存(官方实测 1.34GB 镜像不加速 36s、加速后 4s)。记下缓存对象名,Task 2 的 CR 要引用。

- [ ] **Step 8: Commit**

```bash
git add infra/sandbox-image/Dockerfile docs/superpowers/plans/2026-08-03-sandbox-migration-w1.md
git commit -m "feat(sandbox-image): 适配 Agent Sandbox runtime 注入要求——补 bash + 平台清单核对"
```

---

## Task 2: CR 铺设 + credential-proxy 上集群

**Files:**
- Create: `infra/k8s/base/credential-proxy/deployment.yaml`
- Create: `infra/k8s/base/credential-proxy/service.yaml`
- Create: `infra/k8s/base/credential-proxy/kustomization.yaml`
- Create: `infra/k8s/base/sandbox/sandboxset.yaml`
- Create: `infra/k8s/base/sandbox/trafficpolicy.yaml`
- Create: `infra/k8s/base/sandbox/kustomization.yaml`
- Modify: `infra/k8s/base/kustomization.yaml`
- Modify: `infra/k8s/overlays/test/kustomization.yaml`

**Interfaces:**
- Consumes: Task 1 推的 `sandbox:<sha>` 镜像
- Produces: 集群内 Service `credential-proxy:8081`(egress 代理端口)+ `SandboxSet` 池,Task 3 的探针打它们

- [ ] **Step 1: 读 compose 里的 credential-proxy 定义,列出必须搬的环境变量**

```bash
grep -n "credential-proxy" -A 30 infra/docker-compose.yml
```

关键三项(compose `infra/docker-compose.yml:434-448`):
- `EXPERT_WORK_CRED_PROXY_DB_DSN` —— 云上指 RDS,从既有 secret 取
- `EXPERT_WORK_CRED_PROXY_EGRESS_PORT: "8081"`
- `EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET` —— **必须与沙箱侧铸 token 用的密钥一致**(supervisor 铸、proxy 验;云上由 `AgentSandboxClient` 铸,见 Task 7)

`EXPERT_WORK_CRED_PROXY_SECRET_STORE_BACKEND` 云上不能用 `local_dev`,查 `services/credential-proxy/src/credential_proxy/settings.py` 看有哪些后端、云上该选哪个,按既有 control-plane 的 secret 接线方式配。

- [ ] **Step 2: 照 control-plane 的 manifest 写 credential-proxy 的 Deployment/Service**

```bash
ls infra/k8s/base/control-plane/
```

照抄那套结构(labels、probes、resources、secret 引用方式),不要另起风格。Service 暴露 8081(egress 代理)。副本数 1 起。

- [ ] **Step 3: 写 SandboxSet CR**

按 Agent Sandbox 文档写 `SandboxSet`:引用 Task 1 的镜像与镜像缓存、池 size 设 1(选型文档:一人规模池 1-2 足够)、规格按平台档位填(1c1g 起,平台会规整到未公开档位,以实际创建成功为准)。

- [ ] **Step 4: 写 TrafficPolicy**

沙箱要能到达 `credential-proxy` 的集群内地址。按 TrafficPolicy(L3/4)放行到 credential-proxy Service 的 ClusterIP 段与 8081 端口。**这一条正是 Task 3 探针 2 要验的东西** —— 写了不代表通。

- [ ] **Step 5: 挂进 kustomize**

`infra/k8s/base/kustomization.yaml` 的 `resources` 加 `- credential-proxy` 和 `- sandbox`;overlay `test` 里补镜像 newTag 与环境值。

- [ ] **Step 6: 干跑校验**

```bash
kubectl kustomize infra/k8s/overlays/test > /tmp/w1-render.yaml
grep -c "kind:" /tmp/w1-render.yaml
grep -n "credential-proxy\|SandboxSet\|TrafficPolicy" /tmp/w1-render.yaml | head
```

Expected: 渲染无报错,三个新对象都在。

- [ ] **Step 7: apply 并等就绪**

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
kubectl apply -k infra/k8s/overlays/test
kubectl -n expert-work rollout status deploy/credential-proxy --timeout=300s
```

Expected: `deployment "credential-proxy" successfully rolled out`。

- [ ] **Step 8: Commit**

```bash
git add infra/k8s/
git commit -m "feat(k8s): credential-proxy 上集群 + Agent Sandbox CR 铺设"
```

---

## Task 3: 风险探针 —— 三个未验证项(门)

**Files:**
- Create: `docs/research/2026-08-XX-sandbox-w1-probe-results.md`(日期填实际执行日)

**Interfaces:**
- Consumes: Task 1 的镜像、Task 2 的 CR 与 credential-proxy Service
- Produces: 三个未验证项的实测结论。**任一不通 → 停下来跟人讨论退兜底(普通 ACS Pod 当沙箱),不要硬做 Task 7+**

- [ ] **Step 1: 装 E2B SDK 并核对真实 API**

```bash
cd services/orchestrator
uv add 'e2b==2.24.0' 'e2b-code-interpreter==2.7.0'
uv run python -c "
import e2b, inspect
from e2b import AsyncSandbox
print('e2b', e2b.__version__)
print('create:', inspect.signature(AsyncSandbox.create))
print('connect:', inspect.signature(AsyncSandbox.connect))
print('methods:', [m for m in dir(AsyncSandbox) if not m.startswith('_')])
"
```

**把真实签名抄进探针报告**。后面 Task 7/8 的代码以这里的输出为准 —— 计划里写的 SDK 调用是按公开文档的假设,SDK 实际形态不一致时**以 SDK 为准**,并在报告里记下差异。

- [ ] **Step 2: 探针 1 —— E2B 数据面经 Ingress 真域名**

W0 PoC 在 port-forward 伪装姿势下数据面 502(API 面/envd/网络/路由表均正常)。现在用真域名重验:

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
kubectl -n expert-work get ingress
```

按 Agent Sandbox 文档确认 gateway 的对外访问方式(Ingress 路径还是独立域名),然后从**集群外**跑:

```python
# /tmp/probe1.py
import asyncio
from e2b import AsyncSandbox

async def main():
    sbx = await AsyncSandbox.create(template="<SandboxSet 名>", domain="<gateway 域名>", api_key="<key>")
    print("created:", sbx.sandbox_id)
    r = await sbx.commands.run("echo HELLO_FROM_SANDBOX")
    print("stdout:", r.stdout, "exit:", r.exit_code)
    await sbx.kill()

asyncio.run(main())
```

Expected: 打印 sandbox id 与 `HELLO_FROM_SANDBOX`,退出码 0。
**失败(502 或超时)→ 门不通**:记录完整错误,停下来讨论。

- [ ] **Step 3: 探针 2 —— microVM 能否访问集群内 Service**

沙箱里直接打 credential-proxy 的集群内地址:

```python
# /tmp/probe2.py —— 在已创建的沙箱里跑
r = await sbx.commands.run(
    "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://credential-proxy.expert-work.svc.cluster.local:8081/ || echo UNREACHABLE"
)
print(r.stdout, r.stderr)
```

Expected: 拿到任意 HTTP 状态码(哪怕 407 —— 代理要求认证正是它活着的证据)。
**输出 `UNREACHABLE` → 门不通**:credential-proxy 要改经 Ingress 暴露,那样多一跳公网、必须加 mTLS,这是设计变更,停下来讨论。

- [ ] **Step 4: 探针 3 —— gateway 吞吐边界**

集群里 gateway 是 1 副本 2c4Gi(`sandbox-system/sandbox-gateway`)。并发创建摸边界:

```python
# /tmp/probe3.py
import asyncio, time
from e2b import AsyncSandbox

async def one(i):
    t = time.monotonic()
    sbx = await AsyncSandbox.create(template="<SandboxSet 名>", domain="<gateway 域名>", api_key="<key>")
    dt = time.monotonic() - t
    await sbx.kill()
    return i, dt

async def main():
    for n in (1, 3, 5, 10):
        t = time.monotonic()
        rs = await asyncio.gather(*(one(i) for i in range(n)), return_exceptions=True)
        errs = [r for r in rs if isinstance(r, Exception)]
        oks = [r[1] for r in rs if not isinstance(r, Exception)]
        print(f"n={n} wall={time.monotonic()-t:.1f}s ok={len(oks)} err={len(errs)} "
              f"p_max={max(oks) if oks else 0:.1f}s")
        for e in errs[:2]:
            print("  err:", type(e).__name__, e)

asyncio.run(main())
```

Expected: n=10 时无错误、单个创建 p_max 在可接受范围。
**大量失败或延迟暴涨 → 不是门,是记录项**:写进报告,gateway 副本/规格按结果调,不阻塞本波。

- [ ] **Step 5: 写探针报告**

`docs/research/<今天>-sandbox-w1-probe-results.md`,每个探针一节:**跑的什么命令、原始输出、结论、对方案的影响**。SDK 真实签名(Step 1)单列一节。

- [ ] **Step 6: Commit**

```bash
git add docs/research/ services/orchestrator/pyproject.toml services/orchestrator/uv.lock
git commit -m "chore(sandbox): W1 风险探针——E2B 数据面/集群内 Service/gateway 吞吐实测"
```

- [ ] **Step 7: 门判定**

探针 1 或 2 不通 → **停止,把报告交给人讨论退兜底**。两个都通 → 继续 Task 7。

---

## Task 4: 拆出 WorkspaceStore(纯重构,零行为变化)

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/workspace_store.py`
- Create: `services/orchestrator/tests/test_workspace_store.py`
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py`(删 5 个 workspace 方法及其 Protocol 声明、`RecordingSupervisorClient` 里对应部分)
- Modify: `services/control-plane/src/control_plane/api/workspace.py:179,224,265`
- Modify: `services/control-plane/src/control_plane/api/sessions.py:498,550,596,651`
- Modify: `services/control-plane/src/control_plane/api/artifacts.py:218`
- Modify: `services/control-plane/src/control_plane/api/uploads.py:158`
- Modify: `services/control-plane/src/control_plane/purge/user_purge.py:440`
- Modify: `services/control-plane/src/control_plane/runtime.py`(加 `build_workspace_store`)
- Modify: `services/control-plane/src/control_plane/app.py`(注入 `app.state.workspace_store`)

**Interfaces:**
- Produces:
  - `WorkspaceStore` Protocol —— 5 个 async 方法:`read_file(*, tenant_id: UUID, user_id: UUID, path: str) -> bytes`、`list_files(*, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]`、`write_file(*, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None`、`delete_file(*, tenant_id: UUID, user_id: UUID, path: str) -> None`、`mark_deleted(*, tenant_id: UUID, user_id: UUID) -> None`
  - `SupervisorWorkspaceStore`(dataclass,字段 `base_url: str`、`timeout_s: float`、`transport: httpx.AsyncBaseTransport | None`、`http: httpx.AsyncClient | None`,与 `HTTPSupervisorClient` 同形)
  - `RecordingWorkspaceStore`(测试假件)
  - `build_workspace_store(url: str | None) -> WorkspaceStore | None`
- 注意方法**改了名**:Protocol 上是 `read_file` 而非 `read_workspace_file`(前缀 `workspace_` 在 `WorkspaceStore` 上是冗余的)。HTTP 路径不变。

- [ ] **Step 1: 写失败测试 —— 新模块的形状**

`services/orchestrator/tests/test_workspace_store.py`:

```python
"""WorkspaceStore 两实现 —— 拆分自 SupervisorClient(波 1 Task 4)。"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from orchestrator.tools.workspace_store import (
    RecordingWorkspaceStore,
    SupervisorWorkspaceStore,
    WorkspaceStore,
)


def test_supervisor_store_satisfies_protocol() -> None:
    store = SupervisorWorkspaceStore(base_url="http://sup")
    assert isinstance(store, WorkspaceStore)


def test_recording_store_satisfies_protocol() -> None:
    assert isinstance(RecordingWorkspaceStore(), WorkspaceStore)


@pytest.mark.asyncio
async def test_read_file_hits_the_same_http_path_as_before() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"hello")

    tenant_id, user_id = uuid4(), uuid4()
    store = SupervisorWorkspaceStore(
        base_url="http://sup", transport=httpx.MockTransport(handler)
    )
    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")

    assert data == b"hello"
    # 路径与拆分前逐字相同 —— 这是"零行为变化"的锚点
    assert str(tenant_id) in seen["url"]
    assert str(user_id) in seen["url"]
```

- [ ] **Step 2: 运行,确认失败**

```bash
cd /Users/mac/src/github/jone_qian/expert-work
uv run pytest services/orchestrator/tests/test_workspace_store.py -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'orchestrator.tools.workspace_store'`

- [ ] **Step 3: 建新模块,搬实现**

先看清要搬什么:

```bash
sed -n '159,199p' services/orchestrator/src/orchestrator/tools/sandbox.py   # Protocol 上的 5 个方法
grep -n "read_workspace_file\|list_workspace_files\|write_workspace_file\|delete_workspace_file\|mark_workspace_deleted" services/orchestrator/src/orchestrator/tools/sandbox.py
```

`workspace_store.py` 结构:模块 docstring 说明它从 `sandbox.py` 拆出的由来 → `WorkspaceStore` Protocol(docstring 逐条搬原方法的,含 J.9/J.15/J-36 引用)→ `SupervisorWorkspaceStore`(HTTP 实现,**URL 与请求体逐字照搬**,`_traced_headers` 从 `sandbox.py` import 复用)→ `RecordingWorkspaceStore`。

**照搬时唯一允许的改动是方法名**(去掉 `workspace_` 前缀)。任何 URL、header、错误处理的顺手"改进"都会让"零行为变化"这个前提破功。

- [ ] **Step 4: 运行,确认通过**

```bash
uv run pytest services/orchestrator/tests/test_workspace_store.py -v
```

Expected: 3 passed

- [ ] **Step 5: 从 sandbox.py 摘掉搬走的部分**

删 `SupervisorClient` Protocol 里的 5 个 workspace 方法声明、`HTTPSupervisorClient` 里的 5 个实现、`RecordingSupervisorClient` 里对应的记录字段与方法。

```bash
wc -l services/orchestrator/src/orchestrator/tools/sandbox.py
```

Expected: 明显低于 799(拆走约 120-150 行)。

- [ ] **Step 6: 加工厂**

`services/control-plane/src/control_plane/runtime.py`,紧挨 `build_supervisor_client` 写:

```python
def build_workspace_store(url: str | None) -> WorkspaceStore | None:
    """Build the workspace-file client from the supervisor's base URL.

    波 1 Task 4 — 工作区文件操作从 ``SupervisorClient`` 拆出。本地/CI 下
    工作区是 docker 卷,只有 supervisor 碰得到,所以这个实现仍走 HTTP;
    波 2 的 ``NasWorkspaceStore`` 会直接读挂载的文件系统。

    ``None`` → 工作区文件端点不可用,与 ``build_supervisor_client`` 同语义。
    """
    if url is None:
        return None
    return SupervisorWorkspaceStore(base_url=url)
```

`app.py` 里照 `resolved_supervisor_client`(`app.py:695`)的模式建 `resolved_workspace_store` 并挂 `app.state.workspace_store`;共享 httpx client 的在位注入(`app.py:1300` 那段 `isinstance` 守卫)一并照做。

- [ ] **Step 7: 8 个调用点改指**

逐个改(行号见 **Files**):`supervisor.read_workspace_file(...)` → `workspace_store.read_file(...)`,其余四个同理。依赖注入处从取 `supervisor_client` 改取 `workspace_store`。

```bash
grep -rn "read_workspace_file\|list_workspace_files\|write_workspace_file\|delete_workspace_file\|mark_workspace_deleted" --include="*.py" services/control-plane/src services/orchestrator/src
```

Expected: 只剩 `workspace_store.py` 内部(HTTP 路径字符串)和 supervisor 服务端。

- [ ] **Step 8: 跑受影响的测试**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/control-plane/tests/test_workspace_api.py \
  services/control-plane/tests/test_sessions_api.py \
  services/control-plane/tests/test_artifacts_api.py \
  services/control-plane/tests/test_uploads_api.py \
  services/control-plane/tests/test_user_purge.py \
  services/orchestrator/tests/test_workspace_store.py -q
```

Expected: 全绿。红了先看是不是测试自己 mock 的是 `supervisor_client` —— 那些 mock 要跟着改指 `workspace_store`。

- [ ] **Step 9: 全量回归**

```bash
uv run pytest services/orchestrator services/control-plane -q 2>&1 | tail -5
```

Expected: 与拆分前同样的通过数(纯重构,数字不该变;新增 3 个)。

- [ ] **Step 10: Commit**

```bash
git add services/orchestrator services/control-plane
git commit -m "refactor(sandbox): 工作区文件操作拆出 WorkspaceStore——Protocol 一拆二第一刀

sandbox.py 799 行(仓内 800 上限)拆走 5 个 workspace 方法。URL/请求体
逐字照搬,唯一改动是方法名去掉冗余的 workspace_ 前缀。control-plane
8 个调用点改指新抽象。零行为变化。"
```

---

## Task 5: `SupervisorClient` → `SandboxRuntime` 改名

**Files:**
- Modify: 全仓 208 处引用(20+ 文件),主体在 `services/orchestrator/src/orchestrator/tools/sandbox.py`、`services/control-plane/src/`、两侧 tests

**Interfaces:**
- Consumes: Task 4 拆完的 `sandbox.py`
- Produces: `SandboxRuntime` Protocol(5 方法:`acquire` / `exec` / `release` / `destroy` / `reap`)、`RecordingSandboxRuntime`。`SandboxSupervisorError` **名字不改**。

- [ ] **Step 1: 数清改名面**

```bash
cd /Users/mac/src/github/jone_qian/expert-work
grep -rn "SupervisorClient" --include="*.py" services/ packages/ | wc -l
grep -rln "SupervisorClient" --include="*.py" services/ packages/
```

记下数字,Step 4 要对。

- [ ] **Step 2: 机械改名**

三个符号,注意**先长后短**否则 `HTTPSupervisorClient` 会被 `SupervisorClient` 的规则误伤:

```bash
FILES=$(grep -rl "SupervisorClient" --include="*.py" services/ packages/)
# 长的先来
perl -pi -e 's/\bRecordingSupervisorClient\b/RecordingSandboxRuntime/g' $FILES
perl -pi -e 's/\bHTTPSupervisorClient\b/HTTPSupervisorRuntime/g' $FILES
perl -pi -e 's/\bSupervisorClient\b/SandboxRuntime/g' $FILES
```

`HTTPSupervisorClient` → `HTTPSupervisorRuntime`:它仍是"打 supervisor 的实现",名字保留 `Supervisor` 是准确的;后缀跟 Protocol 对齐。

- [ ] **Step 3: 改变量名与 docstring**

改名脚本碰不到 `supervisor_client` 这类变量名和注释里的散文。逐个过:

```bash
grep -rn "supervisor_client\|supervisor client\|Supervisor Client" --include="*.py" services/ | head -30
```

`app.state.supervisor_client` → `app.state.sandbox_runtime`;`build_supervisor_client` → `build_sandbox_runtime`;docstring 里"the Sandbox Supervisor operations the tool needs"改成描述抽象而非某个实现。

**`SandboxSupervisorError` 保持原名** —— 确认没被误改:

```bash
grep -rn "SandboxSupervisorError" --include="*.py" services/ | wc -l
```

- [ ] **Step 4: 确认零残留**

```bash
grep -rn "SupervisorClient" --include="*.py" services/ packages/ | grep -v "SandboxSupervisorError"
```

Expected: 无输出。

- [ ] **Step 5: 类型检查 + 全量测试**

```bash
uv run mypy services/orchestrator services/control-plane 2>&1 | tail -3
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/orchestrator services/control-plane -q 2>&1 | tail -3
```

Expected: mypy 0 errors;pytest 通过数与 Task 4 结束时一致。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(sandbox): SupervisorClient 改名 SandboxRuntime——Agent Sandbox 下没有 supervisor 了

纯机械改名,趁引用面最小时做(208 处)。SandboxSupervisorError 名字保留:
它是 tools 节点捕获的稳定契约,改名波及错误处理路径而零收益。"
```

---

## Task 6: `sandbox_backend` 配置项 + 工厂分支

**Files:**
- Modify: `services/control-plane/src/control_plane/settings.py:202` 附近
- Modify: `services/control-plane/src/control_plane/runtime.py`（`build_sandbox_runtime` / `build_workspace_store`）
- Test: `services/control-plane/tests/test_sandbox_backend_factory.py`（新建）

**Interfaces:**
- Consumes: Task 5 的 `SandboxRuntime`、Task 4 的 `build_workspace_store`
- Produces: `Settings.sandbox_backend: Literal["supervisor", "agent_sandbox"] | None`;`build_sandbox_runtime(settings: Settings) -> SandboxRuntime | None`(**签名从 `url: str | None` 改成整个 settings**,因为 agent_sandbox 分支要读多个字段)

- [ ] **Step 1: 写失败测试 —— 三态**

`services/control-plane/tests/test_sandbox_backend_factory.py`:

```python
"""build_sandbox_runtime 的后端选择 —— 波 1 Task 6。"""
from __future__ import annotations

import pytest

from control_plane.runtime import build_sandbox_runtime
from control_plane.settings import Settings
from orchestrator.tools.sandbox import HTTPSupervisorRuntime


def test_none_when_nothing_configured() -> None:
    s = Settings(sandbox_backend=None, sandbox_supervisor_url=None)
    assert build_sandbox_runtime(s) is None


def test_supervisor_backend_builds_http_runtime() -> None:
    s = Settings(sandbox_backend="supervisor", sandbox_supervisor_url="http://sup:8080")
    runtime = build_sandbox_runtime(s)
    assert isinstance(runtime, HTTPSupervisorRuntime)
    assert runtime.base_url == "http://sup:8080"


def test_supervisor_backend_without_url_is_none() -> None:
    """后端选了 supervisor 但没给 URL —— 保持现有降级语义,不炸。"""
    s = Settings(sandbox_backend="supervisor", sandbox_supervisor_url=None)
    assert build_sandbox_runtime(s) is None


def test_agent_sandbox_backend_not_wired_yet() -> None:
    """Task 7 之前 agent_sandbox 分支还没实现 —— 明确抛,不静默返 None。"""
    s = Settings(sandbox_backend="agent_sandbox")
    with pytest.raises(NotImplementedError, match="agent_sandbox"):
        build_sandbox_runtime(s)


def test_legacy_url_only_still_works() -> None:
    """老配置只设了 URL 没设 backend —— 视作 supervisor,不破坏现网。"""
    s = Settings(sandbox_backend=None, sandbox_supervisor_url="http://sup:8080")
    assert isinstance(build_sandbox_runtime(s), HTTPSupervisorRuntime)
```

- [ ] **Step 2: 运行,确认失败**

```bash
uv run pytest services/control-plane/tests/test_sandbox_backend_factory.py -v
```

Expected: FAIL —— `Settings` 没有 `sandbox_backend` 字段。

- [ ] **Step 3: 加配置字段**

`services/control-plane/src/control_plane/settings.py`,紧挨 `sandbox_supervisor_url`(第 202 行):

```python
    #: 波 1 —— 沙箱后端选择。``"supervisor"`` 走本地 docker supervisor
    #: (``sandbox_supervisor_url``);``"agent_sandbox"`` 走 ACS Agent
    #: Sandbox(E2B SDK)。``None`` 时按老行为推断:设了
    #: ``sandbox_supervisor_url`` 即视作 ``"supervisor"``,否则沙箱能力关闭。
    sandbox_backend: Literal["supervisor", "agent_sandbox"] | None = None
    #: Agent Sandbox 接入 —— gateway 域名(E2B SDK 的 ``domain``)。
    sandbox_e2b_domain: str | None = None
    #: Agent Sandbox 接入 —— API key。
    sandbox_e2b_api_key: str | None = None
    #: Agent Sandbox 接入 —— SandboxSet 模板名(池领取的来源)。
    sandbox_e2b_template: str | None = None
```

- [ ] **Step 4: 改工厂签名与分支**

```python
def build_sandbox_runtime(settings: Settings) -> SandboxRuntime | None:
    """按 ``sandbox_backend`` 选沙箱运行时实现。

    ``None`` → ``exec_python`` 等沙箱工具不可用;声明了沙箱工具的 agent
    在构建期失败并给出明确错误(既有降级路径,波 1 不改)。
    """
    backend = settings.sandbox_backend
    if backend is None:
        backend = "supervisor" if settings.sandbox_supervisor_url else None
    if backend == "agent_sandbox":
        raise NotImplementedError(
            "sandbox_backend='agent_sandbox' 尚未接线(波 1 Task 7)"
        )
    if backend == "supervisor" and settings.sandbox_supervisor_url:
        return HTTPSupervisorRuntime(base_url=settings.sandbox_supervisor_url)
    return None
```

`app.py:695` 的调用点从 `build_sandbox_runtime(resolved_settings.sandbox_supervisor_url)` 改成 `build_sandbox_runtime(resolved_settings)`。

- [ ] **Step 5: 运行,确认通过**

```bash
uv run pytest services/control-plane/tests/test_sandbox_backend_factory.py -v
```

Expected: 5 passed

- [ ] **Step 6: 回归**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/control-plane -q 2>&1 | tail -3
```

Expected: 全绿(`test_checkpointer_wiring.py` 等碰工厂的测试要跟着改指新签名)。

- [ ] **Step 7: Commit**

```bash
git add services/control-plane
git commit -m "feat(sandbox): 加 sandbox_backend 配置项与工厂分支

工厂签名从 url 改成整个 settings(agent_sandbox 分支要读多个字段)。
老配置只设 sandbox_supervisor_url 仍按 supervisor 走,不破坏现网。
agent_sandbox 分支先明确抛 NotImplementedError,不静默返 None。"
```

---

## Task 7: `AgentSandboxClient` —— acquire / release / destroy + 热会话 CAS

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`
- Create: `services/orchestrator/tests/test_agent_sandbox.py`
- Create: `packages/expert-work-persistence/migrations/versions/0141_sandbox_warm_unique.py`
- Modify: `services/control-plane/src/control_plane/runtime.py`(接上 agent_sandbox 分支)
- Modify: `services/orchestrator/pyproject.toml`(E2B 依赖)

**Interfaces:**
- Consumes: Task 3 探针报告里记的 **E2B SDK 真实签名**、Task 5 的 `SandboxRuntime`、Task 6 的工厂与配置
- Produces: `AgentSandboxClient`(dataclass,字段 `domain: str`、`api_key: str`、`template: str`、`store: SandboxInstanceStore`、`egress_token_secret: str`、`egress_proxy_host: str`、`egress_proxy_port: int`),实现 `acquire` / `release` / `destroy`;`exec` 与 `reap` 在 Task 8/9

- [ ] **Step 1: 写迁移 —— 热会话部分唯一索引**

```python
"""0141 — sandbox warm-session unique index (波 1 Task 7).

一个 ``(tenant, user)`` 同时只该有一个活跃热沙箱。并发 acquire 靠这个
部分唯一索引定单赢家:两路都 ``INSERT ... ON CONFLICT DO NOTHING
RETURNING``,拿到行的建沙箱,没拿到的读赢家的 ``container_id`` 直接
connect。与 triggers program 的"端点建唯一行、两路 CAS 同行单赢家"
同一配方。

非 CONCURRENTLY 建索引(仓内惯例):``sandbox_instance`` 是低写表
(每次 acquire 一行),进部署 runbook 记一笔即可。
"""
from __future__ import annotations

from alembic import op

revision = "0141_sandbox_warm_unique"
down_revision = "0140_token_usage_audit_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
          ON sandbox_instance (tenant_id, user_id)
          WHERE state = 'IN_USE' AND destroyed_at IS NULL AND user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sandbox_instance_warm_unique")
```

`user_id IS NOT NULL` 是必须的:`user_id` 可空(临时沙箱无持久工作区),Postgres 里多行 NULL 不冲突但语义上不该被这个索引管。

- [ ] **Step 2: 跑迁移验证**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests -k migration -q 2>&1 | tail -3
```

Expected: 通过。仓内有迁移链单头校验,确认 0141 接在 0140 后。

- [ ] **Step 3: 写失败测试 —— acquire 的三条不变式**

`services/orchestrator/tests/test_agent_sandbox.py`:

```python
"""AgentSandboxClient —— E2B SDK 实现的 SandboxRuntime(波 1 Task 7/8/9)。

SDK 用假件替身:真实 SDK 调用在契约测试(Task 10)与端到端(Task 11)覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from orchestrator.tools.agent_sandbox import AgentSandboxClient


@dataclass
class FakeCommands:
    calls: list[tuple[str, int | None]] = field(default_factory=list)
    result_stdout: str = ""
    result_stderr: str = ""
    result_exit: int = 0

    async def run(self, cmd: str, timeout: int | None = None):  # noqa: ANN201
        self.calls.append((cmd, timeout))
        return type(
            "R", (), {"stdout": self.result_stdout, "stderr": self.result_stderr,
                      "exit_code": self.result_exit}
        )()


@dataclass
class FakeFiles:
    written: list[tuple[str, bytes | str]] = field(default_factory=list)

    async def write(self, path: str, data: bytes | str) -> None:
        self.written.append((path, data))


@dataclass
class FakeSandbox:
    sandbox_id: str = "sbx-1"
    killed: bool = False
    commands: FakeCommands = field(default_factory=FakeCommands)
    files: FakeFiles = field(default_factory=FakeFiles)
    envs: dict[str, str] = field(default_factory=dict)

    async def kill(self) -> None:
        self.killed = True


@dataclass
class FakeSdk:
    """替身 SDK —— 记录 create/connect 调用。"""
    created: list[dict] = field(default_factory=list)
    connected: list[str] = field(default_factory=list)
    sandbox: FakeSandbox = field(default_factory=FakeSandbox)
    connect_fails: bool = False

    async def create(self, **kwargs):  # noqa: ANN201
        self.created.append(kwargs)
        return self.sandbox

    async def connect(self, sandbox_id: str):  # noqa: ANN201
        self.connected.append(sandbox_id)
        if self.connect_fails:
            raise RuntimeError("sandbox gone")
        return self.sandbox


@dataclass
class FakeInstanceStore:
    """sandbox_instance 表的替身 —— CAS 语义由 claim_warm 表达。"""
    warm: dict[tuple[UUID, UUID], str] = field(default_factory=dict)
    rows: dict[UUID, dict] = field(default_factory=dict)

    async def claim_warm(self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID) -> str | None:
        """占坑成功返 None;已被别人占返赢家的 container_id。"""
        key = (tenant_id, user_id)
        if key in self.warm:
            return self.warm[key]
        self.warm[key] = ""
        self.rows[sandbox_id] = {"tenant_id": tenant_id, "user_id": user_id}
        return None

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        self.rows[sandbox_id]["container_id"] = container_id
        row = self.rows[sandbox_id]
        self.warm[(row["tenant_id"], row["user_id"])] = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        row = self.rows.pop(sandbox_id, None)
        if row is not None:
            self.warm.pop((row["tenant_id"], row["user_id"]), None)

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        self.warm.pop((tenant_id, user_id), None)


def make_client(sdk: FakeSdk, store: FakeInstanceStore) -> AgentSandboxClient:
    return AgentSandboxClient(
        domain="gw.example.com",
        api_key="k",
        template="expert-work-sandbox",
        store=store,
        sdk=sdk,
        egress_token_secret="s3cret",
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
    )


@pytest.mark.asyncio
async def test_acquire_creates_and_records_container_id() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    sandbox_id = await client.acquire(
        tenant_id=tenant_id, thread_id="t1", user_id=user_id
    )

    assert len(sdk.created) == 1
    assert store.rows[sandbox_id]["container_id"] == "sbx-1"


@pytest.mark.asyncio
async def test_concurrent_acquire_has_one_winner() -> None:
    """第二路 acquire 不新建,connect 到赢家。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 1, "第二路不该再建沙箱"
    assert sdk.connected == ["sbx-1"], "第二路该 connect 赢家"


@pytest.mark.asyncio
async def test_connect_failure_rebuilds() -> None:
    """唤醒失败(库存不足/欠费/保留期过被删)→ 丢弃旧行 → 重建。

    工作区权威在外部存储,重建无损 —— spec § 6.3。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    sdk.connect_fails = True

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 2, "connect 失败后必须重建"
    assert store.rows[sandbox_id]["container_id"] == "sbx-1"


@pytest.mark.asyncio
async def test_seed_files_written_before_first_exec() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(
        tenant_id=uuid4(), thread_id="t1", user_id=uuid4(),
        seed_files=(("skills/a.md", b"hello"),),
    )

    assert sdk.sandbox.files.written == [("/workspace/skills/a.md", b"hello")]


@pytest.mark.asyncio
async def test_destroy_kills_and_marks_row() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    sandbox_id = await client.acquire(
        tenant_id=tenant_id, thread_id="t1", user_id=user_id
    )
    await client.destroy(sandbox_id=sandbox_id, reason="ops")

    assert sdk.sandbox.killed is True
    assert sandbox_id not in store.rows
```

- [ ] **Step 4: 运行,确认失败**

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'orchestrator.tools.agent_sandbox'`

- [ ] **Step 5: 加 E2B 依赖**

```bash
cd services/orchestrator
uv add 'e2b==2.24.0' 'e2b-code-interpreter==2.7.0'
```

在 `pyproject.toml` 的依赖旁写清钉版本的理由:

```toml
    # 波 1 —— ACS Agent Sandbox 接入。官方配方钉 <2.25.0(2.25 起的
    # breaking change 与 Agent Sandbox 的组件版本矩阵不兼容),见
    # docs/superpowers/specs/2026-08-03-sandbox-migration-design.md § 八.5
    "e2b==2.24.0",
    "e2b-code-interpreter==2.7.0",
```

- [ ] **Step 6: 写实现**

`agent_sandbox.py`。**SDK 调用以 Task 3 探针报告里记的真实签名为准**,下面是按公开文档的形态:

```python
"""``AgentSandboxClient`` —— ACS Agent Sandbox 上的 :class:`SandboxRuntime`。

E2B SDK 打底。与 ``HTTPSupervisorRuntime`` 的分工:那个打本地 docker
supervisor(开发/CI),这个打云上平台。两者由 ``build_sandbox_runtime``
按 ``sandbox_backend`` 选,上层工具零感知。

热会话(spec § 6.2):``(tenant, user)`` 的活跃沙箱记在 ``sandbox_instance``
表,E2B sandbox id 存在既有的 ``container_id`` 列(同一语义:外部运行时给
的实例标识)。并发 acquire 靠 0141 的部分唯一索引定单赢家。

唤醒失败(spec § 6.3):``connect`` 会因库存不足/欠费/保留期已过被平台删
而失败 —— 丢弃该行、重新 ``create``。工作区权威在外部存储,重建无损。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from expert_work.common.egress_token import mint_egress_token

from orchestrator.tools.sandbox import EgressContext, SandboxSupervisorError

logger = logging.getLogger(__name__)

#: 沙箱内工作区挂载点 —— 与 supervisor 实现一致。
WORKSPACE_ROOT = "/workspace"


class SandboxInstanceStore(Protocol):
    """``sandbox_instance`` 表上 ``AgentSandboxClient`` 需要的操作。"""

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> str | None:
        """占 ``(tenant, user)`` 的热会话坑。

        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` 的封装:占到返
        ``None``(调用方负责建沙箱),没占到返赢家的 ``container_id``
        (调用方 connect 上去)。
        """

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        """回填 E2B sandbox id。"""

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        """标记销毁并让出热会话坑。"""

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """丢弃一个失效的热会话行(唤醒失败后重建前调)。"""


@dataclass
class AgentSandboxClient:
    """:class:`SandboxRuntime` 的 Agent Sandbox 实现。"""

    domain: str
    api_key: str
    template: str
    store: SandboxInstanceStore
    egress_token_secret: str
    egress_proxy_host: str
    egress_proxy_port: int
    #: 测试缝 —— 注入 SDK 替身。None 时用真实 ``AsyncSandbox``。
    sdk: object | None = None
    egress_token_ttl_s: int = 3600

    def _sdk(self) -> object:
        if self.sdk is not None:
            return self.sdk
        from e2b import AsyncSandbox

        return AsyncSandbox

    def _egress_env(self, egress: EgressContext | None) -> dict[str, str]:
        """出网环境变量 —— 与 supervisor 的 ``_egress_env`` 同语义。

        沙箱靠标准 Basic proxy auth 认证到 credential-proxy;token 由
        共享的 ``mint_egress_token`` 铸,密钥必须与 proxy 侧的
        ``EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET`` 一致。
        """
        if egress is None or egress.policy in (None, "none"):
            return {}
        token = mint_egress_token(
            self.egress_token_secret,
            tenant_id=str(egress.tenant_id),
            sandbox_id=str(egress.sandbox_id),
            expires_at=time.time() + self.egress_token_ttl_s,
            allowlist=egress.allowlist,
            denylist=egress.denylist,
        )
        proxy_url = f"http://{token}:@{self.egress_proxy_host}:{self.egress_proxy_port}"
        no_proxy = f"{self.egress_proxy_host},localhost,127.0.0.1"
        return {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        sandbox_id = uuid4()
        existing: str | None = None
        if user_id is not None:
            existing = await self.store.claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )
        sdk = self._sdk()

        if existing:
            try:
                sbx = await sdk.connect(existing)
            except Exception:
                # spec § 6.3 —— 唤醒失败必须能重建,不能把 run 打死。
                logger.warning(
                    "warm sandbox connect failed, rebuilding", exc_info=True
                )
                if user_id is not None:
                    await self.store.drop_warm(tenant_id=tenant_id, user_id=user_id)
                    await self.store.claim_warm(
                        tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                    )
                sbx = await self._create(sdk, egress)
        else:
            sbx = await self._create(sdk, egress)

        for relpath, data in seed_files:
            await sbx.files.write(f"{WORKSPACE_ROOT}/{relpath}", data)

        await self.store.set_container_id(
            sandbox_id=sandbox_id, container_id=sbx.sandbox_id
        )
        return sandbox_id

    async def _create(self, sdk: object, egress: EgressContext | None) -> object:
        try:
            return await sdk.create(
                template=self.template,
                domain=self.domain,
                api_key=self.api_key,
                envs=self._egress_env(egress),
            )
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox create failed: {exc}") from exc

    async def release(self, *, sandbox_id: UUID) -> None:
        """常规拆除 —— 让平台按休眠保留期回收,不主动 kill。

        与 supervisor 实现的差异:那边 ``release`` 是 ``docker rm``;这里
        沙箱进入平台的休眠流程(内存态保留,下次 acquire 唤醒 1-10s)。
        热会话行保留,``container_id`` 就是下次 connect 的凭据。
        """
        return None

    async def _attach(self, sandbox_id: UUID) -> object:
        """按内部 id 连上沙箱。``destroy`` 与 ``exec``(Task 8)共用。"""
        container_id = await self.store.get_container_id(sandbox_id=sandbox_id)
        if container_id is None:
            raise SandboxSupervisorError(f"unknown sandbox {sandbox_id}")
        try:
            return await self._sdk().connect(container_id)
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox attach failed: {exc}") from exc

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        """强制拆除 —— 真 kill 沙箱并让出热会话坑。"""
        container_id = await self.store.get_container_id(sandbox_id=sandbox_id)
        if container_id is not None:
            try:
                sbx = await self._sdk().connect(container_id)
                await sbx.kill()
            except Exception:
                # 沙箱已不在(保留期过/被平台回收)—— 仍要往下清行,
                # 否则热会话坑永远占着,该 (tenant, user) 再也 acquire 不到。
                logger.info("destroy: sandbox %s already gone", container_id)
        await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)
```

`SandboxInstanceStore` Protocol 相应补一个方法:

```python
    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """读某行的 E2B sandbox id;行不存在或未回填返 ``None``。"""
```

`FakeInstanceStore` 跟着加(`return self.rows.get(sandbox_id, {}).get("container_id")`)。

- [ ] **Step 7: 运行,确认通过**

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -v
```

Expected: 5 passed

- [ ] **Step 8: 变异验证 —— 三条不变式真被咬住**

逐个改坏、确认对应测试转红、再改回:

1. `acquire` 里 `existing` 分支直接跳过 connect 永远 `_create` → `test_concurrent_acquire_has_one_winner` 必须 FAIL
2. connect 的 `except` 改成 `raise` → `test_connect_failure_rebuilds` 必须 FAIL
3. `seed_files` 循环删掉 → `test_seed_files_written_before_first_exec` 必须 FAIL

**三个都必须真的转红**。有测试在变异下仍绿,说明它没咬住不变式,补强它再继续。

- [ ] **Step 9: 接上工厂分支**

`runtime.py` 里把 Task 6 的 `NotImplementedError` 换成真实构造(读 `sandbox_e2b_*` 三个配置 + egress 三项 + 注入 `SandboxInstanceStore` 实现);`test_sandbox_backend_factory.py::test_agent_sandbox_backend_not_wired_yet` 改成断言返回 `AgentSandboxClient`。

`SandboxInstanceStore` 的真实实现(SQL 版 `claim_warm` 等)写在 `packages/expert-work-persistence` 的 sandbox_instance store 里,照仓内既有 store 的写法。

- [ ] **Step 10: SQL store 的真容器集成测**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests -k "sandbox_instance and warm" -v
```

必须有一条**真并发**用例:两个协程同时 `claim_warm` 同一 `(tenant, user)`,断言恰好一个拿到 `None`、另一个拿到赢家 id。in-memory 假件不校验唯一索引,这条只有真 Postgres 能验。

- [ ] **Step 11: Commit**

```bash
git add services/orchestrator services/control-plane packages/expert-work-persistence
git commit -m "feat(sandbox): AgentSandboxClient 生命周期——acquire/release/destroy + 热会话 CAS

E2B SDK 打底,sandbox id 存既有 container_id 列(不加新列)。并发 acquire
靠 0141 部分唯一索引定单赢家;connect 失败(库存/欠费/保留期过)丢行重建,
工作区权威在外部存储所以无损。egress 复用共享的 mint_egress_token,密钥与
credential-proxy 侧一致。"
```

---

## Task 8: `AgentSandboxClient.exec` —— 四个契约点

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`
- Modify: `services/orchestrator/tests/test_agent_sandbox.py`

**Interfaces:**
- Consumes: Task 7 的 `AgentSandboxClient`
- Produces: `exec(*, sandbox_id: UUID, code: str, timeout_s: int | None) -> SandboxOutcome`

**契约来源**:`infra/sandbox-image/runner.py:28-72`。每次 exec 是 `subprocess.run([sys.executable, "-I", "-c", code])` —— 新进程、隔离模式、状态不保持。"热"的是容器和 `/workspace` 文件,不是 Python 变量。

- [ ] **Step 1: 写失败测试 —— 四个契约点各一条**

追加到 `test_agent_sandbox.py`:

```python
from orchestrator.tools.agent_sandbox import (
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_S,
)


@pytest.mark.asyncio
async def test_exec_clamps_timeout_high() -> None:
    """契约 1:timeout clamp 到 [1, 300](runner.py:51)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=9999)

    _, timeout = sdk.sandbox.commands.calls[-1]
    assert timeout == MAX_TIMEOUT_S == 300


@pytest.mark.asyncio
async def test_exec_clamps_timeout_low_and_defaults() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=0)
    assert sdk.sandbox.commands.calls[-1][1] == 1

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=None)
    assert sdk.sandbox.commands.calls[-1][1] == DEFAULT_TIMEOUT_S == 30


@pytest.mark.asyncio
async def test_exec_truncates_output_at_one_million_chars() -> None:
    """契约 2:输出上限 1_000_000 chars(runner.py:37)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.sandbox.commands.result_stdout = "x" * (MAX_OUTPUT_CHARS + 500)
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    outcome = await client.exec(sandbox_id=sid, code="print('x'*2_000_000)", timeout_s=5)

    assert len(outcome.stdout) == MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_exec_timeout_maps_to_minus_one_and_flag() -> None:
    """契约 3:超时 → exit_code=-1, timed_out=True(runner.py:60-66)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    async def boom(cmd: str, timeout: int | None = None):
        raise TimeoutError("deadline")

    sdk.sandbox.commands.run = boom  # type: ignore[method-assign]
    outcome = await client.exec(sandbox_id=sid, code="import time;time.sleep(99)", timeout_s=1)

    assert outcome.exit_code == -1
    assert outcome.timed_out is True


@pytest.mark.asyncio
async def test_exec_writes_code_to_file_not_shell_arg() -> None:
    """已知偏差(spec § 6.1):code 不能拼进命令行(引号注入)。

    先 files.write 到临时文件再 `python -I <file>`。副作用是 `-c` 模式下
    `__file__` 不存在、文件模式下存在 —— 这条差异由此测试钉住。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    nasty = "print('it\\'s \"quoted\"; rm -rf /')"
    await client.exec(sandbox_id=sid, code=nasty, timeout_s=5)

    written_paths = [p for p, _ in sdk.sandbox.files.written]
    assert any(p.endswith(".py") for p in written_paths), "code 必须先落文件"
    cmd, _ = sdk.sandbox.commands.calls[-1]
    assert nasty not in cmd, "code 绝不能出现在命令行里"
    assert "python -I " in cmd
```

- [ ] **Step 2: 运行,确认失败**

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -k exec -v
```

Expected: FAIL —— `AgentSandboxClient` 没有 `exec`

- [ ] **Step 3: 实现**

```python
#: 契约常量 —— 与 infra/sandbox-image/runner.py:28-37 逐字对齐。
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 1_000_000


    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None
    ) -> SandboxOutcome:
        """在沙箱里跑 ``code``,返回与 supervisor 实现同形的结果。

        四个契约点(spec § 6.1,源头是 runner.py):timeout clamp
        ``[1, 300]`` 缺省 30;输出截 1M chars;超时 ``exit_code=-1,
        timed_out=True``;响应固定 4 键。

        code 先写临时文件再执行,不拼进命令行 —— 拼进去会被引号注入。
        """
        effective = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        effective = max(1, min(effective, MAX_TIMEOUT_S))

        sbx = await self._attach(sandbox_id)
        script = f"/tmp/ew-exec-{uuid4().hex}.py"
        await sbx.files.write(script, code)
        try:
            result = await sbx.commands.run(f"python -I {script}", timeout=effective)
        except TimeoutError:
            return SandboxOutcome(stdout="", stderr="", exit_code=-1, timed_out=True)
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox exec failed: {exc}") from exc
        return SandboxOutcome(
            stdout=result.stdout[:MAX_OUTPUT_CHARS],
            stderr=result.stderr[:MAX_OUTPUT_CHARS],
            exit_code=result.exit_code,
            timed_out=False,
        )
```

`_attach(sandbox_id)` 是个私有 helper:从 store 读 `container_id` → `sdk.connect`。Task 7 的 `destroy` 也用它,一并抽出来。

**注意**:E2B SDK 的超时抛什么异常,以 Task 3 探针 Step 1 的实测为准 —— 不一定是内置 `TimeoutError`,可能是 SDK 自己的异常类。按实测改 `except` 子句,并在这里写一行注释记下真实类型。

`SandboxOutcome` 的确切字段名先核对:

```bash
sed -n '63,73p' services/orchestrator/src/orchestrator/tools/sandbox.py
```

- [ ] **Step 4: 运行,确认通过**

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -v
```

Expected: 10 passed

- [ ] **Step 5: 变异验证**

1. clamp 那行改成 `effective = timeout_s or DEFAULT_TIMEOUT_S`(去掉上下界)→ 两条 clamp 测试必须 FAIL
2. 截断 `[:MAX_OUTPUT_CHARS]` 去掉 → 截断测试必须 FAIL
3. `commands.run(f"python -I -c '{code}'")` 改成拼命令行 → 注入测试必须 FAIL

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator
git commit -m "feat(sandbox): AgentSandboxClient.exec——四个契约点与 runner.py 逐字对齐

timeout clamp [1,300] 缺省 30 / 输出截 1M chars / 超时 exit_code=-1
timed_out=True / 固定 4 键。code 先落临时文件再 python -I 执行,不拼命令行
(引号注入);副作用是 __file__ 语义与 -c 模式有别,测试钉住。"
```

---

## Task 9: `AgentSandboxClient.reap`

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`
- Modify: `services/orchestrator/tests/test_agent_sandbox.py`

**Interfaces:**
- Consumes: Task 8 后的 `AgentSandboxClient`
- Produces: `reap(*, force: bool) -> int`

`reap` 不能只靠平台的休眠保留期 —— 运维强制拆除与 M0→M1 Gate E2E 都依赖它的确定性语义(`force=True` 拆掉每个活跃会话)。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_reap_force_kills_every_active_sandbox_of_ours() -> None:
    """force=True 拆掉表里记着的每个活跃沙箱,返回拆除数。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    t1, t2 = uuid4(), uuid4()
    await client.acquire(tenant_id=t1, thread_id="a", user_id=uuid4())
    sdk.sandbox = FakeSandbox(sandbox_id="sbx-2")
    await client.acquire(tenant_id=t2, thread_id="b", user_id=uuid4())

    reaped = await client.reap(force=True)

    assert reaped == 2
    assert store.rows == {}


@pytest.mark.asyncio
async def test_reap_ignores_sandboxes_not_ours() -> None:
    """SDK 列出的沙箱里有别人的 —— 只拆 sandbox_instance 表里记着的。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.foreign = ["sbx-someone-else"]
    client = make_client(sdk, store)
    await client.acquire(tenant_id=uuid4(), thread_id="a", user_id=uuid4())

    assert await client.reap(force=True) == 1
```

`FakeSdk` 要加 `list()`(返回自家 + `foreign` 的 id 列表)和 `foreign: list[str]` 字段。

`SandboxInstanceStore` Protocol 要加 `list_active() -> list[tuple[UUID, str]]`(返回 `(sandbox_id, container_id)`),`FakeInstanceStore` 跟着实现。

- [ ] **Step 2: 运行,确认失败**

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -k reap -v
```

Expected: FAIL —— 没有 `reap`

- [ ] **Step 3: 实现**

```python
    async def reap(self, *, force: bool) -> int:
        """扫掉空闲(或 ``force`` 时全部)热会话,返回拆除数。

        以 ``sandbox_instance`` 表为准而非 SDK 的 ``list()`` —— 同一账号下
        可能有别的来源创建的沙箱,拆掉不属于我们的会误伤。
        """
        rows = await self.store.list_active(only_idle=not force)
        reaped = 0
        for sandbox_id, container_id in rows:
            try:
                sbx = await self._sdk().connect(container_id)
                await sbx.kill()
            except Exception:
                # 沙箱已不在(保留期过/被平台回收)也要清行,否则热会话坑
                # 永远占着,该 (tenant, user) 再也 acquire 不到。
                logger.info("reap: sandbox %s already gone", container_id)
            await self.store.mark_destroyed(sandbox_id=sandbox_id, reason="reap")
            reaped += 1
        return reaped
```

`list_active(only_idle: bool)` 的 SQL 实现:`only_idle=True` 时按 `last_used_at` 早于空闲 TTL 过滤(与 supervisor 的 reaper 同口径,看 `services/sandbox-supervisor/src/sandbox_supervisor/reaper.py` 的阈值来源)。

- [ ] **Step 4: 运行,确认通过**

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -v
```

Expected: 12 passed

- [ ] **Step 5: 变异验证**

`except` 里的 `mark_destroyed` 挪进 `try` → 沙箱已消失时行不被清 → 加一条测试(connect 抛异常仍要清行)必须 FAIL。这条不变式很容易在重构中丢,值得单独一个测试。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator
git commit -m "feat(sandbox): AgentSandboxClient.reap——以表为准而非 SDK list

同账号下可能有别的来源创建的沙箱,按 SDK list 拆会误伤。沙箱已消失时
仍要清热会话行,否则该 (tenant,user) 永远 acquire 不到。"
```

---

## Task 10: 契约测试 —— 一套用例两个实现

**Files:**
- Create: `services/orchestrator/tests/test_sandbox_runtime_contract.py`

**Interfaces:**
- Consumes: `HTTPSupervisorRuntime`(Task 5)、`AgentSandboxClient`(Task 7-9)
- Produces: 参数化夹具 `runtime`,两个实现共用一套断言

**这是防两套实现漂移的唯一手段**,spec § 九把它定为本波核心质量手段。

- [ ] **Step 1: 写契约套件**

```python
"""SandboxRuntime 契约测试 —— 一套用例两个实现(spec § 九)。

supervisor 实现真跑 docker(需 DOCKER_HOST);agent_sandbox 实现真连测试
集群(需 E2B 凭据)。缺任一环境就 skip 对应参数,但 **CI 里必须有一档
真连测试集群跑** —— 否则漂移只会在生产暴露。
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _supervisor_runtime():
    url = os.environ.get("EXPERT_WORK_SANDBOX_SUPERVISOR_URL")
    if not url:
        pytest.skip("EXPERT_WORK_SANDBOX_SUPERVISOR_URL 未设 —— supervisor 契约档跳过")
    from orchestrator.tools.sandbox import HTTPSupervisorRuntime

    return HTTPSupervisorRuntime(base_url=url)


def _agent_sandbox_runtime():
    api_key = os.environ.get("EXPERT_WORK_SANDBOX_E2B_API_KEY")
    if not api_key:
        pytest.skip("E2B 凭据未设 —— agent_sandbox 契约档跳过")
    dsn = os.environ.get("EXPERT_WORK_DB_DSN")
    if not dsn:
        pytest.skip("EXPERT_WORK_DB_DSN 未设 —— 契约档需要真 sandbox_instance 表")

    from sqlalchemy.ext.asyncio import create_async_engine

    from expert_work.persistence.sandbox_instance import SqlSandboxInstanceStore
    from orchestrator.tools.agent_sandbox import AgentSandboxClient

    return AgentSandboxClient(
        domain=os.environ["EXPERT_WORK_SANDBOX_E2B_DOMAIN"],
        api_key=api_key,
        template=os.environ["EXPERT_WORK_SANDBOX_E2B_TEMPLATE"],
        store=SqlSandboxInstanceStore(engine=create_async_engine(dsn)),
        egress_token_secret=os.environ.get(
            "EXPERT_WORK_EGRESS_TOKEN_SECRET", "contract-test-secret"
        ),
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
    )



@pytest.fixture(params=["supervisor", "agent_sandbox"])
def runtime(request):
    return {"supervisor": _supervisor_runtime,
            "agent_sandbox": _agent_sandbox_runtime}[request.param]()


@pytest.mark.asyncio
async def test_exec_returns_stdout(runtime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c1")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="print('CONTRACT_OK')", timeout_s=30
        )
        assert "CONTRACT_OK" in outcome.stdout
        assert outcome.exit_code == 0
        assert outcome.timed_out is False
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.asyncio
async def test_exec_nonzero_exit_is_reported(runtime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c2")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import sys; sys.exit(3)", timeout_s=30
        )
        assert outcome.exit_code == 3
        assert outcome.timed_out is False
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.asyncio
async def test_exec_timeout_contract(runtime) -> None:
    """契约 3 在两个实现上必须一致:exit_code=-1 且 timed_out=True。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c3")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import time; time.sleep(30)", timeout_s=2
        )
        assert outcome.timed_out is True
        assert outcome.exit_code == -1
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.asyncio
async def test_stderr_captured(runtime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c4")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import sys; print('to-err', file=sys.stderr)",
            timeout_s=30,
        )
        assert "to-err" in outcome.stderr
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.asyncio
async def test_seed_files_land_in_workspace(runtime) -> None:
    sid = await runtime.acquire(
        tenant_id=uuid4(), thread_id="c5",
        seed_files=(("seeded.txt", b"SEED_CONTENT"),),
    )
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="print(open('/workspace/seeded.txt').read())",
            timeout_s=30,
        )
        assert "SEED_CONTENT" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.asyncio
async def test_workspace_files_survive_across_exec(runtime) -> None:
    """"热"的是文件系统而非 Python 变量 —— 两个实现都该如此。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c6")
    try:
        await runtime.exec(
            sandbox_id=sid,
            code="open('/workspace/persisted.txt','w').write('STILL_HERE')",
            timeout_s=30,
        )
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="print(open('/workspace/persisted.txt').read())",
            timeout_s=30,
        )
        assert "STILL_HERE" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.asyncio
async def test_python_variables_do_not_survive_across_exec(runtime) -> None:
    """反过来:变量不保持(runner.py 每次新起 `python -I` 进程)。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c7")
    try:
        await runtime.exec(sandbox_id=sid, code="X = 42", timeout_s=30)
        outcome = await runtime.exec(
            sandbox_id=sid, code="print('X' in dir())", timeout_s=30
        )
        assert "False" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")
```

- [ ] **Step 2: 跑 supervisor 那档**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
docker compose -f infra/docker-compose.yml --profile sandbox up -d
export EXPERT_WORK_SANDBOX_SUPERVISOR_URL=http://localhost:<supervisor 端口>
uv run pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -k supervisor -v
```

端口从 compose 里查:`grep -n "sandbox-supervisor" -A 15 infra/docker-compose.yml | grep ports -A 2`。

Expected: 7 passed。**红了先怀疑契约描述写错了,而不是急着改实现** —— supervisor 是现网行为的基准。

- [ ] **Step 3: 跑 agent_sandbox 那档**

```bash
export EXPERT_WORK_SANDBOX_E2B_API_KEY=<key>
export EXPERT_WORK_SANDBOX_E2B_DOMAIN=<gateway 域名>
export EXPERT_WORK_SANDBOX_E2B_TEMPLATE=<SandboxSet 名>
uv run pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -k agent_sandbox -v
```

Expected: 7 passed。**任何一条两档行为不一致 —— 那就是漂移,当场修**,不要用 skip 绕过。

- [ ] **Step 4: CI workflow 接一档**

新建 `.github/workflows/sandbox-contract.yml`,照 `sandbox-gvisor.yml` 的路径过滤写法,跑 agent_sandbox 档(凭据从 GitHub Secrets 取)。路径过滤至少覆盖 `services/orchestrator/src/orchestrator/tools/**` 与 `infra/sandbox-image/**`。

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/tests/test_sandbox_runtime_contract.py .github/workflows/sandbox-contract.yml
git commit -m "test(sandbox): SandboxRuntime 契约测试——一套用例两个实现

7 条契约:stdout/非零退出/超时语义/stderr/seed_files/文件跨 exec 保持/
变量跨 exec 不保持。两档行为不一致即漂移,CI 接一档真连测试集群。"
```

---

## Task 11: 端到端验收

**Files:**
- Modify: `infra/k8s/overlays/test/kustomization.yaml`(newTag)
- Create: `docs/superpowers/plans/2026-08-03-sandbox-migration-w1.md` 的验收记录追加

**Interfaces:**
- Consumes: 前 10 个任务的全部产出
- Produces: 波 1 验收结论,决定波 2 是否开工

- [ ] **Step 1: 发测试环境**

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
TAG=$(git rev-parse --short=8 HEAD)
tools/deploy/build-push.sh --images control-plane --tag "$TAG" --push
```

改 overlay 的 control-plane newTag,补上 `sandbox_backend: agent_sandbox` 与三个 `sandbox_e2b_*` 配置(configmap/secret),然后:

```bash
kubectl -n expert-work delete job migrate --ignore-not-found
kubectl apply -k infra/k8s/overlays/test
kubectl -n expert-work wait --for=condition=complete job/migrate --timeout=300s
kubectl -n expert-work rollout status deploy/control-plane --timeout=300s
```

migrate Job 日志里要看到 `0140 -> 0141_sandbox_warm_unique`。

- [ ] **Step 2: 验收一 —— 真 agent 跑 exec_python**

在调试台建一个声明了 `exec_python` 的 agent,发一条要它算东西的消息(例:「用 Python 算 1 到 100 的和并打印」)。

Expected: 工具卡显示 `exec_python` 调用、输出含 `5050`、run 正常结束。

**失败排查顺序**:control-plane 日志有无 `sandbox create failed` → `sandbox-system` 里 gateway 日志 → `kubectl -n expert-work get sandbox`(或平台的 CR 名)看沙箱有没有真被创建。

- [ ] **Step 3: 验收二 —— 出网经 credential-proxy 且审计落表**

给 agent 一条要它访问外部 HTTP 的指令(用 allowlist 里已有的域名),然后查审计:

```bash
kubectl -n expert-work exec deploy/control-plane -- python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    e = create_async_engine(os.environ['EXPERT_WORK_DB_DSN'])
    async with e.connect() as c:
        r = await c.execute(text(
            'SELECT created_at, host, port, bytes_out, bytes_in '
            'FROM sandbox_egress_audit ORDER BY created_at DESC LIMIT 5'))
        for row in r:
            print(row)
asyncio.run(main())
"
```

Expected: 至少一行新审计,host 是刚才访问的域名。
**零行 → 出网没经过 proxy**:检查沙箱的 `HTTPS_PROXY` env 有没有真注入(`AgentSandboxClient._egress_env` 的 `egress` 参数是不是 `None` —— 它由 `bind_egress` 注入,确认这条链在云实现下没断)。

- [ ] **Step 4: 验收三 —— 热会话复用**

同一个用户连发两条要跑 Python 的消息,查表:

```bash
kubectl -n expert-work exec deploy/control-plane -- python -c "
... SELECT id, container_id, state, created_at FROM sandbox_instance
    WHERE user_id = '<那个 user_id>' ORDER BY created_at DESC LIMIT 5
"
```

Expected: 第二次没有新建行,或新行的 `container_id` 与第一次相同(走了 connect 而非 create)。

- [ ] **Step 5: 验收四 —— reap 能拆干净**

```bash
kubectl -n expert-work exec deploy/control-plane -- curl -sS -X POST \
  localhost:8000/internal/sandboxes/reap -d '{"force": true}' -H 'content-type: application/json'
```

(端点路径以 `services/control-plane/src/control_plane/api/sandboxes.py` 里实际的为准。)

Expected: 返回拆除数;再查 `sandbox_instance` 无活跃行。

- [ ] **Step 6: 记录验收结果**

在本计划文件末尾追加一节「波 1 验收记录」:四条验收各自的实际结果、踩到的坑、波 2 要注意的事。

- [ ] **Step 7: Commit + 开 chore(deploy) 记录 PR**

```bash
git add infra/k8s/overlays/test/kustomization.yaml docs/superpowers/plans/
git commit -m "chore(deploy): 测试环境 sandbox_backend=agent_sandbox——波 1 验收"
```

照仓内惯例(#1075 / #1099)开 `chore(deploy)` 记录 PR。

---

## Self-Review

**1. Spec 覆盖检查**

| spec 要求 | 落在 |
|---|---|
| § 4.1 Protocol 一拆二 | Task 4(拆 WorkspaceStore)+ Task 5(改名) |
| § 4.2 四个实现 | Task 4(两个 WorkspaceStore)+ Task 5/7/8/9(两个 SandboxRuntime) |
| § 4.3 工厂 + `sandbox_backend` | Task 6 |
| § 4.4 8 个调用点改指 | Task 4 Step 7 |
| § 五波 1 基建(镜像/CR/proxy 上集群) | Task 1 + Task 2 |
| § 六.1 exec 四契约点 + `__file__` 偏差 | Task 8 |
| § 6.2 热会话 CAS + 复用 `container_id` | Task 7 Step 1/3/6 |
| § 6.3 唤醒失败重建 | Task 7 Step 3(`test_connect_failure_rebuilds`)+ Step 6 |
| § 6.5 错误处理统一抛 `SandboxSupervisorError` | Task 7 `_create` / Task 8 `exec` |
| § 八 三个未验证项 | Task 3(门) |
| § 九 契约测试三档 | Task 10 |
| 波 1 验收标准 | Task 11 |

`seed_files` 注入(spec § 6.1 末段)→ Task 7 Step 3 有测试、Step 6 有实现、Task 10 有跨实现契约。无遗漏。

**2. 占位符扫描**:无 TBD/TODO。三处"以实测为准"是有意的 —— E2B SDK 的真实签名、超时异常类型、平台镜像要求清单,都在 Task 1 Step 2 / Task 3 Step 1 有明确的获取动作和记录去处,不是把决定甩给实施者。

**3. 类型一致性**:`SandboxRuntime`(Task 5 产出)在 Task 6/7/10 用法一致;`WorkspaceStore` 五方法名(`read_file`/`list_files`/`write_file`/`delete_file`/`mark_deleted`)在 Task 4 定义后无他处引用旧名;`SandboxInstanceStore` 的四个方法(`claim_warm`/`set_container_id`/`mark_destroyed`/`drop_warm`)在 Task 7 定义,Task 9 追加 `list_active`,`FakeInstanceStore` 同步;`DEFAULT_TIMEOUT_S`/`MAX_TIMEOUT_S`/`MAX_OUTPUT_CHARS` 在 Task 8 Step 3 定义、Step 1 的测试从模块 import,一致。

---

## 波 1 验收记录(2026-08-04)

发布:`e9ab7ca7` → 测试环境(`infra/k8s/overlays/test`,control-plane 2 副本)。
migrate Job 跑到 `0140_token_usage_audit_grant -> 0141_sandbox_warm_unique`。
`tools/deploy/smoke.sh test` 9/9 PASS。

### 前置:配置与 Secret 接线

configmap-patch 加三项(`EXPERT_WORK_SANDBOX_BACKEND=agent_sandbox` /
`_E2B_DOMAIN` / `_E2B_TEMPLATE`),control-plane-secrets 加两项
(`EXPERT_WORK_SANDBOX_E2B_API_KEY` / `EXPERT_WORK_SANDBOX_EGRESS_TOKEN_SECRET`)。
后者与 credential-proxy 侧的 `EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET`
**已比对确认字节一致** —— 不一致时两边都不会在启动时报错,只会表现为每次出网 407。

### 验收结果

| # | 验收项 | 结果 |
|---|---|---|
| 0 | 配置→工厂→SDK→集群 直连 | ✅ `acquire` 0.3s(池领取)/ `exec` 出 `5050` / `destroy` ok |
| 1 | 真 agent 跑 `exec_python` | ✅ 工具真被调、标「成功」、答出 5050、run 正常结束(glm-5.2,2 步,22.6s);⚠️ 调试台结果区显示不出 stdout / 退出码,见「发现五」 |
| 2 | 出网经 credential-proxy 且审计落表 | ✅ HTTPS `verdict='allowed'` bytes_up=1820 bytes_down=36247;⚠️ 明文 HTTP 407,见「发现三」 |
| 3 | 热会话复用 | ✅ 第二次 `acquire` 0.05s(第一次 0.47s),返回同一 `sandbox_id`,`container_id` 相同,`last_used_at` 被写 |
| 4 | reap 拆干净 | ✅ `reaped_count=2`,全表 `destroyed_at IS NULL` 归零(含一条崩溃遗留的孤儿行) |

验收 0 是本计划没有的一步,插在验收 1 之前:它绕开 LLM 决策,只验
**配置→工厂→E2B SDK→集群** 这条链。链先通再谈端到端,失败时定位范围小一个数量级。

验收 3 顺带在真栈上证实了 Task 9 的 I-2 修复(`last_used_at` 此前从未被写,
空闲清扫实际按 `acquired_at` 扫,会杀活跃会话)。验收 4 清掉的孤儿行里有一条
正是验收 2 脚本中途崩溃留下的 `container_id IS NULL` 行 —— Task 9 I-1 修的
`list_stuck_creating` 正是为这类行存在的,真栈上验到了。

### 验收 1 的执行方式

由人在调试台完成,不是自动化的。走 HTTP API 自动化要 token,而 Keycloak 的
`expert-work-admin-ui` client 未开 direct access grants:

```
400 {"error":"unauthorized_client",
     "error_description":"Client not allowed for direct access grants"}
```

开它是账户设置变更,且真 agent 还需要租户已配 LLM 凭据(凭据只由人从 admin UI
粘进加密金库)。两项都超出本任务能自行决定的范围。

实测结果:agent 发「用 Python 算 1 到 100 的和并打印」,LLM 选择调
`exec_python`,入参 `total = sum(range(1, 101))`,工具卡标「成功」,最终答案
5050,run 正常结束。这一项独有的增量 —— **build_fn 注入 →
`_EgressBindingClient` 包装 → tools 节点捕获 `SandboxSupervisorError`** 这段
agent 构建期接线 —— 由此覆盖。

### 发现一:control-plane 镜像构建断在 `kruise-agents`(已修 `e9ab7ca7`)

```
Failed to download and build `kruise-agents @ git+https://github.com/...`
Git executable not found. Ensure that Git is installed and available.
```

Task 7 把 `kruise-agents` 加进依赖时锁的是 GitHub rev(该包无 PyPI 发布),
`uv sync` 拉 git 依赖要 shell 出去调 git 二进制,而 uv 的
`python3.12-bookworm-slim` 基础镜像不带 git。**只在 Docker 里复现** —— 开发机
永远有 git,所以一路活到第一次真构镜像。修法见该 commit。

连带的持续风险:这条依赖让**镜像构建期强依赖 GitHub 可达**。构建机在国内网络
且无代理时会断。波 4 上线前应考虑 vendor 进仓或推一份到内部 registry。

### 发现二:本计划 Task 11 Step 3 的审计 SQL 列名全错

计划里写的 `created_at, host, port, bytes_out, bytes_in` 在
`sandbox_egress_audit` 上一个都不存在。真列名(见
`packages/expert-work-persistence/src/expert_work/persistence/models/credential_proxy.py:82`):

```sql
SELECT occurred_at, target_host, target_port, verdict, bytes_up, bytes_down, error_msg
FROM sandbox_egress_audit ORDER BY occurred_at DESC LIMIT 5;
```

计划里那几个名字是照语义猜的,没对过模型 —— 照抄会以为"审计没落表"而去查一个
根本不存在的故障。

### 发现三:明文 HTTP 走 egress proxy 恒 407(既有缺陷,非本波引入)

沙箱内实测:

```
https://www.baidu.com -> 200                        # 通
http://www.baidu.com  -> HTTPError 407              # 不通
```

根因在 stdlib:`urllib.request.ProxyHandler.proxy_open` 只在
`if user and password:` 时才加 `Proxy-Authorization`,而我们的 proxy URL 是
`http://<token>:@host:8081` —— 密码为空串,falsy,头永远不加。HTTPS 走
`CONNECT`,由沙箱镜像的 `sitecustomize.py` patch `set_tunnel` 补上了头,所以反而通。

**不是迁移引入的**:`supervisor.py:811` 的 `proxy_url` 与
`agent_sandbox.py:449` 逐字相同,同一个镜像、同一套 env,两个后端行为必然一致。
影响面也有限 —— `requests`/`httpx`/`urllib3` 都会自己发头,只有 stdlib urllib
打明文 HTTP 这一个组合中招。

本波不修(Global Constraints 写明 supervisor 冻结,且修它要动镜像里的
`sitecustomize`,影响两个后端)。修法有两条:给 `sitecustomize` 再 patch 一层
明文 HTTP 路径,或把 proxy URL 的空密码换成占位符让 stdlib 的
`user and password` 成立。**后者一行,且对两个后端同时生效,是推荐做法。**

### 发现四:工厂在配置缺项时静默降级

`build_sandbox_runtime`(`runtime.py:1470-1476`)在
`sandbox_backend="agent_sandbox"` 但三项 E2B 配置任一为空时 `return None`,
表现为"声明了 `exec_python` 的 agent 构建期失败",而不是"沙箱配置不完整"。
配错一个字母时的排查体验很差。波 2 值得改成显式抛错。

### 发现五:调试台显示不出沙箱工具的 stdout 与退出码(既有缺陷,非本波引入)

验收 1 的工具卡标「成功」、答案也对,但展开「结果」只有一个红色的
`退出码: ?`,stdout 一行不显示。

根因是两个既有机制相撞:

1. `builder.py:2830` 对每个工具结果做 spotlight(PI-1b 间接注入防御),
   其中 `datamark()`(`expert-work-common/spotlight.py:57`)把**每一段空白
   替换成 `▁ `** —— 换行首当其冲。
2. 前端 `parseExecResult`(`apps/admin-ui/src/api/tool_timeline.ts:76`)是
   **从渲染后的文本里正则抠**结构:`/\nexit_code:\s*(-?\d+)\s*$/` 找退出码,
   `indexOf("stdout:\n")` 找 stdout 段。

datamark 之后一个 `\n` 都不剩,两个都必然失配:

```
后端 ToolResult.content   'stdout:\n1 到 100 的和是: 5050\n\n\nexit_code: 0'   ← 正则匹配得到 0
前端拿到的 preview        'stdout:▁ 1▁ 到▁ 100▁ 的和是:▁ 5050▁ exit_code:▁ 0'  ← 正则 None,find 返回 -1
```

`stripFence` 只剥 `«UNTRUSTED nonce=…»` 标记,不还原 datamark(datamark 本就
不可逆 —— `\n\n\n` 和 ` ` 都变成同一个 `▁ `)。

**不是迁移引入的**:`datamark` / `format_sandbox_outcome` / `parseExecResult`
三处本波都没碰,supervisor 后端走同一条渲染+spotlight 路径,现象必然一样。
这个环境此前从没真跑过 `exec_python`(sandbox-supervisor 从未上过集群),
所以这是它第一次被看见。

修法(前端为主,follow-up 单独做):

- **退出码**:改从 `ToolMessage.artifact` 读 —— 那正是 `ToolResult.meta`
  (`builder.py:2834` 注释写明),里面 `exit_code` / `timed_out` / `truncated`
  都是结构化的,前端也已经在读 `m.artifact` 拿 `trigger_id`。从被防御机制
  改写过的文本里正则抠结构,本来就是脆的。
- **stdout / stderr**:artifact 里目前没有,要显示得让后端把两段也放进
  `meta`。这一步跨前后端,值得和上一条一起做成一个 follow-up。

### 给波 2 的注意事项

1. **SandboxSet 仍在 `default` namespace**,control-plane 在 `expert-work`。
   沙箱 id 形如 `default--expert-work-sandbox-<suffix>`。跨 ns 访问
   credential-proxy 靠 FQDN(短名解不出),配置默认值已经是 FQDN,别改成短名。
2. **`acr-pull` Secret 在两个 ns 各有一份**(Secret 不能跨 ns 引用)。把
   SandboxSet 挪进 `expert-work` 时这份复制品可以删。
3. 镜像缓存(`ImageCache` CRD)这个集群仍没有 —— 冷启 35~40s 全花在拉 2.46GB
   镜像上,池领取则是 0.05s。`replicas` 保持 ≥1 是当前唯一的规避手段。
4. 工作区仍是 E2B 临时盘,跨 run 不保留 —— 波 2 挂 NAS 才补齐。
