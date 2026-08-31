# Stream F — Sandbox（设计先行）

> 落实 [docs/ITERATION-PLAN.md](../ITERATION-PLAN.md) § Stream F（M0；F.1 – F.7）。
> 执行的是 [architecture/subsystems/14-sandbox-pool](../architecture/subsystems/14-sandbox-pool.md)、
> [architecture/subsystems/11-credential-proxy](../architecture/subsystems/11-credential-proxy.md)、
> [research/02-sandbox-isolation.md](../research/02-sandbox-isolation.md)、
> [adr/0007-secret-store.md](../adr/0007-secret-store.md)
> 的 M0 子集；同时落 24 P0 中的 **#2（服务认证 — sandbox 出站 mTLS）、#9（at-rest 加密 — secret KMS）、#25（cancellation 第 3 段 — sandbox kill）**。
>
> 本 Stream **不**重做 LangGraph 执行器 / 中间件链 / 工具注册表 / SecretStore 抽象 —— 这些在 Stream E / F.6 抽象层已建好，
> F 在它们之上拼出"LLM 生成的代码在 gVisor 沙盒里安全执行"的端到端能力 + 凭证零落地 + 取消即杀。

设计先行规则（[memory:feedback_design_first_iteration.md](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/feedback_design_first_iteration.md)）：
所有架构 / 接口 / mini-ADR 必须在编码前就锁定，F.1 – F.7 PR 仅执行本文档。

> **顺序硬性要求**：F 内部按"镜像 → runtime 抽象 → supervisor 服务 → 工具接入 → 凭证链路 → 取消末端"
> 做 bottom-up。任何"先接 `exec_python` 工具再补镜像加固"的捷径都会触发返工，因为加固清单（非 root /
> read-only rootfs / cap-drop / no-new-privileges）一旦在工具跑通后才补，前期所有验证用例都要重跑。

---

## 1. 范围 & 边界

### 1.1 In-scope（F.1 – F.11）

> F.8 – F.11 为 Stream F 推进中识别的设计细化新增（ITERATION-PLAN 原列 F.1 – F.7）：F.8 补验收门自动化 harness，F.9 – F.11 补 egress 网络隔离链路。

| 子项 | 实现内容 | 关联子系统 / P0 |
|------|---------|-----------------|
| **F.1 Sandbox Supervisor 服务** | 新服务 `services/sandbox-supervisor/`：内网 HTTP API `acquire / release / destroy`；M0 **冷启动版** —— 每次 `acquire` 直接 `docker run`，**无 warm pool**（subsystem 14 § 9 M0）。`sandbox_instance` 表已建（migration），为 M1 warm pool 铺路；状态机简化为 `CREATING → IN_USE → DESTROYED`（M0 无 READY 回池态）。配额仅做 `tenant_quota.max_sandboxes` 简单计数校验。 | subsystem 14；P0 n/a |
| **F.2 单 Python 沙盒镜像** | `infra/sandbox-image/Dockerfile`：minimal Python 3.12-slim + 限定库（stdlib + 少量白名单包，**无** `pip` / 编译器 / `sudo` / `curl`）。强制 Mini-ADR F-5 的 6 条加固清单。`exec_python` 运行体是镜像内一个长驻 PID 1 的 `runner.py`（读 stdin JSON → exec → 写 stdout JSON），无 shell 注入面。 | subsystem 14 § 5.6 |
| **F.3 Docker + gVisor (runsc) 启动路径** | `SandboxRuntimeProvider` 抽象：dev（macOS / Linux dev）注入 `runc`，prod（Linux）注入 `runsc`（gVisor）。`docker run` 参数固化加固 flag（`--read-only` / `--cap-drop=ALL` / `--security-opt=no-new-privileges` / `--pids-limit` / `--memory` / `--cpus` / `--network`）。 | subsystem 14 § 5.5；research/02 |
| **F.4 `exec_python` 工具接入 Orchestrator** | `services/orchestrator/src/orchestrator/tools/sandbox.py`：`exec_python` 实现通用 `Tool` Protocol（`async def call(args) -> ToolResult`）；args = `{code: str, timeout_s: int}`；经 Supervisor `acquire` 拿 sandbox → 把 `code` 送进 `runner.py` → 收 `{stdout, stderr, exit_code}` → `release`。注册为 `BuiltinToolSpec`（Stream E 判别联合）。**同 PR 把 E.10 `sandbox_audit_middleware` 装进 `before_tool_dispatch` 链**（E.10 中间件已实现，链装配 ITERATION-PLAN 明确"随 Stream F sandbox 工具接入"）。输出截断见 Mini-ADR F-9。 | subsystem 14 § 1；Stream E E.10 cross-stream |
| **F.5 Credential Proxy（aiohttp 自研版）** | 新服务 `services/credential-proxy/`：aiohttp 反向代理，`POST /forward`（`X-Expert-Work-Upstream` + `X-Expert-Work-Secret-Ref` header）。`secret_allowlist` 表 + manifest 加载时校验；进程内 LRU（容量 1万 / TTL 60s）；注入审计写 `credential_proxy_audit` 表（**绝不记明文 secret**）。secret 后端 = F.6 的 `SecretStore`（**不直连 Vault** —— ADR-0007 已定 M0 走阿里云 KMS）。M0 sandbox 出站唯一放行目标，见 Mini-ADR F-2。 | subsystem 11；P0 #2 |
| **F.6 SecretStore `aliyun_kms` 后端** | `SecretStore` 抽象层 + `make_secret_store` factory + `LocalDevSecretStore` 在 Stream E 已落（`packages/expert-work-runtime/.../secret_store/`）。本子项实现 ADR-0007 § 2.1 缺口：`AliyunKmsSecretStore` adapter（`make_secret_store("aliyun_kms")` 当前 `raise NotImplementedError`）+ 短 TTL 缓存包装（static 60s / dynamic 取 TTL 一半）。 | adr/0007；P0 #9 |
| **F.7 请求取消的 sandbox kill 信号** | E.15 的 `CancellationToken`（`config["configurable"]` 通道、协作式）触达 `exec_python` 工具：token 取消 → 工具 `run_cancellable` 竞速中断 → 调 Supervisor `destroy(reason="cancelled")` → `docker kill`（SIGKILL）容器并回收资源，**≤1s 内杀干净**。Supervisor 侧 TTL 兜底：`IN_USE` 且 `now - acquired_at > timeout_s + 30s` 强制 destroy。 | P0 #25 第 3 段；Stream E E.15 cross-stream |
| **F.8 沙盒 Docker 集成测试 harness** | `services/sandbox-supervisor/tests/test_supervisor_integration.py`：进程内 `SandboxSupervisor` + 真实 `CliDockerClient`（runc），自动化验收门 #1/#2/#4/#5/#8（测试矩阵 #45/#48/#50/#56/#57/#59）。设计细化新增。 | Mini-ADR F-10 F-11 F-12 |
| **F.9 sandbox egress 网络隔离** | `expert-work-sandbox-egress` 创建为 Docker `--internal` 网络 —— 沙盒结构性无对外路由（公网 / 云元数据 / 内网全不可达），唯一可达 = 同 internal 网上的 credential-proxy。关闭验收门 #3（测试矩阵 #49）。**不依赖 compose**（harness 用 stub proxy）。设计细化新增。 | subsystem 21；Mini-ADR F-14 |
| **F.10 credential-proxy 容器化 + 入 docker-compose** | 给 credential-proxy 写正式多阶段 uv 构建 Dockerfile（确立"Expert Work 服务容器化"pattern，credential-proxy 作 pilot，其余 3 服务复用见 ITERATION-PLAN I.1）；`infra/docker-compose.yml` 加 `credential-proxy` 服务 + `expert-work-sandbox-egress`（internal）/ egress 双网络，proxy 双归属。设计细化新增。 | Mini-ADR F-15 |
| **F.11 control-plane 接 `ToolEnv.supervisor_client`** | control-plane 生产构造 `ToolEnv` 时注入 `HTTPSupervisorClient`（base URL 取自 settings）—— 否则 manifest 声明 `exec_python` 直接 `AgentFactoryError`。`#60` 全栈 egress e2e 移入 ITERATION-PLAN **I.1**（需真 proxy + postgres + 迁移全栈，在 harness 重建迷你栈不划算）。设计细化新增。 | Stream E E.6 |

### 1.2 Out-of-scope（明确推迟）

| 推迟项 | 落地 Stream | 备注 |
|-------|------------|------|
| Warm pool（每 image 维度 idle 池、acquire / release 复用）| M1-A | subsystem 14 § 9 M1；M0 接受 1-3s 冷启动 |
| 镜像构建缓存（`layer_key` 算法、私有 registry push）| M1-A | M0 本地 `docker build`，单镜像无 layer cache |
| `bash` / `read_file` / `ls` 等多工具沙盒面 | M1-F | Mini-ADR F-1；M0 单 `exec_python` 足够 |
| 多节点 bin-packing / reaper job / `dedicated_node` | M1-A / M2 | M0 单节点 |
| Credential Proxy 升级 Envoy + Lua / Vault dynamic secrets | M1-C | subsystem 11 § 9 M1；M0 aiohttp 自研 |
| secret 自动轮换（监听 KMS / Vault lease 事件）| M1-C | M0 靠短 TTL 缓存自然过期 + 重拉 |
| sandbox 状态 checkpoint / 跨节点恢复 | M2 | subsystem 14 § 9 M2 |
| K8s Pod + RuntimeClass=gvisor / Kata | M3 | research/02；M0/M1 docker-compose |
| GPU 沙盒 / 持久化文件系统 / 快照 | M3 | M0 纯计算 + 临时 tmpfs |
| `dangling_tool_call` 处理（取消打断 tool 留 orphan）| Stream E E.15 已覆盖 | `GraphRunner.sanitize_thread` 在 resume 前补 placeholder ToolMessage |

### 1.3 验收门（来自 ITERATION-PLAN § Stream F Verification）

落实 research/02 §"沙盒安全验证"7 条用例 + ITERATION-PLAN 取消用例，共 8 条：

1. **文件系统隔离** — sandbox A 写 `/workspace/secret.txt` → `release` 后 sandbox B `acquire` 同 image，读不到该文件（每次 `docker run` 全新容器 + tmpfs `/workspace`）。
2. **进程隔离** — sandbox A 启后台进程 → sandbox B `ps aux` 不可见（独立 PID namespace）。
3. **网络隔离** — sandbox 内连 `169.254.169.254`（云元数据）、`host.docker.internal`、Postgres 内网 IP → **全部 connection refused**；仅 `credential-proxy.internal:443` 可达（Mini-ADR F-2）。
4. **secret 不可见** — sandbox 内 `env`、`/proc/self/environ`、`cat /run/secrets/*` → 无任何真实凭证（凭证只在 Credential Proxy 出站链路注入）。
5. **fork bomb PID limit** — sandbox 内 fork bomb → 被 `--pids-limit` 终止，不影响其他 sandbox / 宿主。
6. **timing / side-channel** — gVisor syscall 拦截下，宿主 binary 无法被 `perf` fingerprint（prod runsc 路径）。
7. **逃逸 PoC** — 跑 CVE-2019-5736（runc）PoC → 在 runsc 下**必须失败**。
8. **取消即杀** — 发起 long-running `exec_python`（`while True: pass`），触发 cancellation → sandbox 在 **≤1s 内**被 `docker kill` 干净、`sandbox_instance.state=DESTROYED`、资源回收。

> **验证状态约定**（同 Stream E 收尾）：用例 1/2/4/5/8 由 **F.8** 集成 harness 在 CI（runc 即可覆盖隔离语义，对应测试矩阵 #45/#48/#50/#56/#57）跑自动化集成测试；用例 3（egress 隔离）由 **F.9** 用 Docker `--internal` 网络实现并自动化（测试矩阵 #49，Mini-ADR F-14）；用例 6/7 依赖 **真实 runsc 环境**（macOS 不可用 gVisor，CI 在 Linux runner 跑），归入 **M0→M1 Gate 的沙盒渗透测试**。
>
> **Stream K.K5 Gate Exit Criteria 锁定**（[STREAM-K-DESIGN § 3.K5](./STREAM-K-DESIGN.md)）：用例 6（timing / side-channel）与用例 7（CVE-2019-5736 PoC）已在 [ITERATION-PLAN.md § M0→M1 Gate Exit Criteria](../ITERATION-PLAN.md) 显式列为必跑通条款 —— **gVisor 7/7 沙盒安全用例在 staging Linux 全部跑通**。"软推迟" 不再被接受：不跑通 = Gate 不能过。本节"归入 Gate"是落地路径，**不是豁免**（[memory:complete-not-minimal](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/feedback_complete_not_minimal.md) / [memory:no-design-choice-disguise](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/feedback_no_design_choice_disguise.md)）。

---

## 2. 架构

### 2.1 服务边界 — `sandbox-supervisor` / `credential-proxy` 两个新服务

Stream E 已确立 control-plane ↔ orchestrator 是**进程内单体**（control-plane import orchestrator 库）。Stream F 新增**两个独立进程服务**：

```
┌─────────────────────────────────────────────┐
│ control-plane 进程（含 orchestrator 库）       │
│   exec_python Tool ──┐                        │
└──────────────────────┼────────────────────────┘
                       │ 内网 HTTP (mTLS)
                       │ acquire / release / destroy
                       ▼
        ┌──────────────────────────┐      docker API
        │  sandbox-supervisor 服务  │ ───────────────► Docker Engine + runsc
        └──────────────────────────┘                        │
                                                             │ docker run
                                                             ▼
                                            ┌─────────────────────────────┐
                                            │  sandbox 容器（gVisor）       │
                                            │   PID 1 = runner.py          │
                                            │   出站仅 → credential-proxy   │
                                            └──────────────┬───────────────┘
                                                           │ POST /forward
                                                           ▼
                                            ┌──────────────────────────┐
                                            │  credential-proxy 服务    │
                                            │   SecretStore (阿里云 KMS) │
                                            └──────────────────────────┘
```

**为什么 supervisor / proxy 是独立进程而非并进 control-plane**（Mini-ADR F-3）：
- supervisor 调 Docker API + 管子进程生命周期，是 IO + 阻塞密集型，并进会拖累 orchestrator 的 `asyncio` 事件循环。
- proxy 是 sandbox 出站的**网络边界** —— 它必须和 sandbox 在不同 network namespace，逻辑上就不能并进。
- ITERATION-PLAN F.1 已明确 `services/sandbox-supervisor/` 目录。
- M0 单机仍可三服务同机部署（docker-compose），独立进程 ≠ 独立主机。

### 2.2 Sandbox 生命周期（M0 冷启动版）

```
acquire ──► CREATING ──(docker run + 健康检查 ok)──► IN_USE ──┐
   │            │                                            │ release / destroy / cancel / TTL
   │            └──(docker run 失败)──► FAILED                 ▼
   │                                                      DESTROYED  (docker rm -f)
```

M0 **不做** subsystem 14 的 `READY` 回池态 —— 每次 `release` 直接 `docker rm -f`，下次 `acquire` 全新 `docker run`。这天然满足验收门 #1（文件隔离）—— 没有复用就没有残留。`sandbox_instance` 表仍按 subsystem 14 § 3.2 建全字段，M1 warm pool 直接复用。

### 2.3 镜像 & 加固清单（F.2）

`infra/sandbox-image/Dockerfile`，强制 subsystem 14 § 5.6：

```dockerfile
FROM python:3.12-slim
RUN useradd -u 10000 -m agent          # 非 root
COPY runner.py /opt/runner.py
USER agent
ENTRYPOINT ["python", "/opt/runner.py"]   # exec form，无 shell
```

`docker run` 侧固化 flag（由 `SandboxRuntimeProvider` 注入）：

```
--read-only                       根文件系统只读
--tmpfs /workspace:rw,size=64m     仅 /workspace + /tmp 可写（tmpfs，随容器销毁）
--cap-drop=ALL                     无 capability
--security-opt=no-new-privileges
--pids-limit=128                   fork bomb 兜底
--memory=512m --cpus=1.0           资源上限
--network=expert-work-sandbox-egress     仅可达 credential-proxy（见 F-2）
--runtime=runsc                    prod；dev 省略 = runc
```

### 2.4 `exec_python` 工具接入 Orchestrator（F.4）

`exec_python` 是 Stream E `BuiltinToolSpec` 判别联合下的一个 builtin。调用链：

```
agent_node (LLM 产 tool_call: exec_python{code, timeout_s})
   │
   ▼ before_tool_dispatch anchor
sandbox_audit_middleware  ── 黑名单命中 (os.system / 写 /etc / socket 直连) ──► 拒绝 + audit
   │ 通过
   ▼
exec_python.call(args)
   ├─ supervisor.acquire(tenant, image, resources, thread_id)
   ├─ runner 协议：send {code} → recv {stdout, stderr, exit_code}
   ├─ 输出截断（Mini-ADR F-9）
   ├─ supervisor.release(sandbox_id)   （finally，含异常 / 取消路径）
   └─► ToolResult(content=..., meta={exit_code, truncated})
```

`sandbox_audit_middleware`（E.10）在本 PR 首次"真跑"：它只对 `exec_python` 触发，HTTP / MCP / web_search 跳过（E.10 设计已写明）。它审查的是 `code` 字符串（AST 白名单 + 危险模式黑名单），不是 shell 命令 —— M0 工具是 `exec_python` 不是 `bash`（Mini-ADR F-1）。

### 2.5 Credential Proxy（F.5）

M0 = subsystem 11 § 9 的 aiohttp 自研版。注入流程（subsystem 11 § 3.3）：

```
[req in] X-Expert-Work-Secret-Ref + X-Expert-Work-Upstream
   ▼
[allowlist_check]  查 secret_allowlist (tenant, agent, version, ref) ── deny ─► 403 + audit
   ▼
[fetch_secret]  LRU hit ─► inject ; miss ─► SecretStore.get(ref) ─► cache ─► inject
   │                                         （后端 = 阿里云 KMS，F.6）
   ▼
[inject]  替换 Authorization header（或 query / body field，按 InjectRule）
   ▼
[forward upstream]  记 latency / status
   ▼
[audit]  写 credential_proxy_audit（ref + tenant + host + status，无明文）
```

M0 与 [Stream E E-7] 的关系：E-7 说"orchestrator 进程内的 `http` builtin 工具 M0 直连、不经 proxy"。本 Stream **不推翻 E-7** —— 那是 orchestrator 进程内链路。F.5 proxy 管的是**另一条链路**：sandbox 容器内的出站。两条链路不冲突（详见 Mini-ADR F-2）。

### 2.6 SecretStore `aliyun_kms` 后端（F.6）

抽象层已在 Stream E 落地（`SecretStore` Protocol + `make_secret_store` factory + `LocalDevSecretStore`）。本子项补 ADR-0007 § 2.1 的生产后端：

```python
# packages/expert-work-runtime/src/expert_work/runtime/secret_store/aliyun_kms.py
class AliyunKmsSecretStore:                 # 实现 SecretStore Protocol
    """阿里云 KMS Secrets Manager 后端 + 短 TTL 进程内缓存。

    经 RAM Role 认证（无需分发凭据）；GetSecretValue 读取。
    缓存 TTL：static 60s；dynamic 取 secret 实际 TTL 的一半。
    """
```

`make_secret_store("aliyun_kms")` 当前 `raise NotImplementedError` —— 本子项把它接上。credential-proxy 与 control-plane 启动加载 LLM key 都经此后端，**不直连阿里云 SDK**。

### 2.7 Cancellation → sandbox kill（F.7）

复用 Stream E E.15 的 `CancellationToken`（协作式、`config["configurable"]` 通道）。
E.15 已落地的事实：`tools_node` 把**整个 tool dispatch** 用 `token.run_cancellable(...)`
包住 —— token 不进 `ToolContext`，工具自己不持有 token；取消时 `run_cancellable`
对 dispatch task 调 `task.cancel()`，`exec_python.call` 的 `await` 收到
`asyncio.CancelledError`（`_invoke_tool` 只 `except Exception`，故穿透）：

```
run_manager.cancel ──► token.set()  （E.15 既有）
   │
   ▼  tools_node: token.run_cancellable(_dispatch_tool(...))  （E.15 既有）
[cancel 抢先] ──► task.cancel()
   │
   ▼  exec_python.call 内 `await client.exec(...)` 收到 asyncio.CancelledError
   ▼  finally 块按 cancelled 标志分流 → supervisor.destroy(reason="cancelled")
supervisor.destroy(sandbox_id, reason="cancelled")
   │
   ▼  docker kill <container>   (SIGKILL，不等 graceful)
sandbox_instance.state = DESTROYED；≤1s（验收门 #8）
```

兜底：调用方崩溃没走到 `destroy` 时，supervisor 的 TTL reaper（M0 = 启动时一个 `asyncio` 周期任务，每 10s 扫一次）杀掉 `IN_USE` 且超 `timeout_s + 30s` 的孤儿容器。

---

## 3. Mini-ADRs

### F-1：M0 沙盒工具面 = 单个 `exec_python`，不做 bash / read_file / ls

- **替代**：照 deer-flow（`sandbox/tools.py`）出 `bash` + `read_file` + `ls` 三件套。
- **选择**：M0 只出一个 `exec_python`（执行 Python 代码片段）。
- **理由**：(1) ITERATION-PLAN F.4 明确写的就是 `exec_python`。(2) Expert Work 是**通用 Agent 执行引擎**，不是编码 Agent 产品；bash/read_file/ls 是编码场景的工具面。(3) 单工具最小化 M0 的 `sandbox_audit` 审查面 —— 只需审 Python AST，不必同时维护 shell 命令前缀白名单。(4) `exec_python` 内可调 `subprocess` / `os` 覆盖大部分"列目录 / 读文件"需求（受 `sandbox_audit` 黑名单约束）。
- **代价**：[velvety-puzzling-rivest 计划] 里 F.4 的 truncation 条款引用了 deer-flow 的 `bash 20k / read_file 50k / ls 20k`；M0 工具是 `exec_python`，故截断收敛为 **stdout / stderr 各 20k**（Mini-ADR F-9）。bash/read_file/ls 多工具面推 M1-F，与 sub-agent 一并做。

### F-2：M0 sandbox 出站 = iptables allowlist 仅放行 credential-proxy，其余 DROP

- **替代 A**：`--network=none`，sandbox 纯计算无出网（最简单）。**替代 B**：放行全部出网。
- **选择**：sandbox 接入专用 docker 网络 `expert-work-sandbox-egress`，iptables 仅放行目标 = `credential-proxy.internal:443`，其余（含 `169.254.169.254` / 内网 / 公网）一律 **DROP**。
- **理由**：(1) 让 F.5 Credential Proxy 在 M0 有**真实消费者** —— 否则 proxy 是死代码。(2) 验收门 #3（网络隔离）才有意义：`--network=none` 下"连 169.254.169.254 失败"是 trivially-true，证明不了 M1+ warm pool / 多工具依赖的 iptables allowlist 真的生效。(3) 与 subsystem 11 § 5.2 / 14 / 21 网络策略设计一致 —— 三份文档都假设 sandbox 经 proxy 出网。(4) DROP 而非 REDIRECT：M0 显式代理，调用方必须显式 POST 到 proxy；透明 REDIRECT 是 M1 Envoy 的事。
- **代价**：M0 比"纯计算沙盒"多一层 netns + iptables 规则。可接受 —— 这层规则本就是验收门 #3 要测的东西。
- **与 E-7 的边界**：E-7 管 orchestrator 进程内 `http` builtin（直连）；F-2 管 sandbox 容器内出站（经 proxy）。两条链路、不冲突。
- **补充（F.9 校正）**：本 ADR 当初写"iptables allowlist"。M0 实际强制机制改为 Docker `--internal` 网络 —— 沙盒 `--cap-drop=ALL` 下无法自管 iptables。详见 Mini-ADR F-14。

### F-3：sandbox-supervisor / credential-proxy 是独立进程服务，不并进 control-plane

- **替代**：把 supervisor 逻辑做成 orchestrator 的一个库模块（沿用 E 的进程内单体）。
- **选择**：两个独立 `services/` 进程。
- **理由**：(1) supervisor 调 Docker API + 阻塞式管子进程，并进会拖 orchestrator 事件循环。(2) proxy 是 sandbox 出站的网络边界，物理上必须独立 netns。(3) ITERATION-PLAN F.1 已写 `services/sandbox-supervisor/`。
- **代价**：多两个进程；M0 靠 docker-compose 同机编排 + mTLS（C.2 既有）互认，运维成本可控。

### F-4：M0 不做 warm pool，每次 acquire 全新 `docker run`

- **替代**：M0 就上 warm pool（subsystem 14 § 5.1 的 EWMA 伸缩）。
- **选择**：M0 冷启动版 —— `acquire` = `docker run`，`release` = `docker rm -f`。
- **理由**：(1) subsystem 14 § 9 M0 明确"不做 warm pool，接受 1-3s 冷启动"。(2) 全新容器天然满足验收门 #1（文件隔离）—— 无复用无残留，省掉"reset 到出厂"的渗透测试风险（subsystem 14 开放问题 #1）。(3) M0 单 agent 低并发，冷启动 1-3s 可接受。
- **代价**：`exec_python` 首字节延迟高（含 `docker run` + 健康检查）。M1-A warm pool 解决；`sandbox_instance` 表 M0 即建全字段铺路。

### F-5：沙盒镜像加固清单 M0 即强制全 6 条，不留后补

- **替代**：M0 先跑通 `exec_python`，加固 flag M1 补齐。
- **选择**：F.2 PR 落地即强制 6 条（非 root / read-only rootfs / cap-drop=ALL / no-new-privileges / 无 sudo+编译器 / exec-form ENTRYPOINT）。
- **理由**：加固清单是验收门 1-7 的前提；后补意味着前期所有安全用例都要重测，且"先松后紧"期间任何 demo 都跑在不安全配置上 —— 这正是 § 0 顺序硬性要求要避免的返工。
- **代价**：F.2 PR 略大（Dockerfile + runner.py + `SandboxRuntimeProvider` 加固 flag 一起出）。可接受。

### F-6：Credential Proxy M0 用 aiohttp 自研，不上 Envoy

- **替代**：M0 直接上 Envoy + Lua filter。
- **选择**：aiohttp 反向代理（subsystem 11 § 9 M0，~1500 行）。
- **理由**：(1) subsystem 11 § 9 已定 M0 aiohttp / M1 Envoy。(2) M0 单机低 QPS，aiohttp 性能够；Envoy 的连接管理 / per-tenant listener 是 M1 多租户生产化才需要。(3) allowlist 校验逻辑用 Python 写比 Lua 可维护。
- **代价**：M1 要做一次 aiohttp→Envoy 迁移；`/forward` 的 HTTP 契约（header 约定）M0 即锁死，迁移时调用方无感。

### F-7：proxy 取 secret 经 `SecretStore` 抽象，不直连 Vault / 阿里云 SDK

- **替代**：proxy 内直接调阿里云 KMS SDK（subsystem 11 § 3.3 字面写的是"Vault GET"）。
- **选择**：proxy 依赖 `SecretStore` Protocol，后端由 `make_secret_store` 注入（M0 = `aliyun_kms`）。
- **理由**：(1) ADR-0007 已定 M0 secret 后端 = 阿里云 KMS，**不是 Vault** —— subsystem 11 写"Vault"是该文档早于 ADR-0007、属 M1 重评估项。(2) 全项目"应用代码不直接绑云 SDK"是 ADR-0007 § 2.2 硬约束。(3) proxy 与 control-plane 共用同一 `SecretStore` 实例语义，缓存策略统一。
- **代价**：subsystem 11 文档的"Vault"措辞与现实有偏差 —— 本 ADR 记录此校正；不单独改 subsystem 11（它的 M1 章节确实在评估 Vault）。

### F-8：cancellation → sandbox 用 `docker kill`（SIGKILL）抢占式，不等 graceful

- **替代**：先给 runner.py 发 SIGTERM 等它 graceful 退出，再 SIGKILL。
- **选择**：取消即 `docker kill`（直接 SIGKILL 整个容器）。
- **理由**：(1) 验收门 #8 要求 ≤1s 杀干净；graceful 宽限期会吃掉这 1s 预算。(2) sandbox 是无状态计算体（tmpfs `/workspace` 随容器销毁），没有"需要 flush 的状态"，graceful 无收益。(3) 与 E.15 整体取消语义一致 —— E.15 对 LLM call 也是竞速中断不等收尾。
- **代价**：runner.py 进程被硬杀，不会写"我被取消了"日志 —— 取消事件由工具侧 / supervisor 侧 audit 记录，容器内不需要。
- **补充（F.8 修正）**：supervisor 的强制 `destroy`（`reason != release`）必须**先 `docker rm --force` 再 `link.close()`**。`link.close()` 等的是 stdin EOF，而忙碌 runner 要等当前 `exec` 返回才会看到 EOF —— 先 close 会吃满 5s grace、击穿 ≤1s 预算。F.8 集成测试 #57 实测发现此序问题并修正（F.7 单测用假桩未覆盖真实 `close()`）。

### F-9：`exec_python` 输出截断 = stdout / stderr 各 20k chars，工具内自管

- **替代**：写一个 `output_truncation_middleware` 统一拦 ToolResult。
- **选择**：`exec_python` 适配器内部截断 stdout / stderr 各 20k chars（尾部截 + 保 exit_code + `ToolResult.meta.truncated=true`）。
- **理由**：对齐 Stream E Mini-ADR E-10（tool output truncation 每工具自管）—— E-10 已为 web_search / HTTP / MCP 定调，`exec_python` 遵循同 pattern，复用 `expert_work.runtime.tools.truncation` 共享 helper。
- **代价**：无额外代价 —— 本就是 E-10 既定 pattern 的延伸。

### F-10：sandbox 验收门自动化 = runc 集成测试（F.8），gVisor-only 门留人工

- **替代**：F.4–F.7 各自落地时就把 § 1.3 的 integration 验收门写成 CI 测试。
- **选择**：F.4–F.7 只落假桩单元测试；§ 1.3 中 **runc 即可覆盖语义**的门（#1/#2/#4/#5/#8 → 测试矩阵 #45/#48/#50/#56/#57）统一由收尾 PR **F.8** 用真实 Docker 集成 harness 自动化；gVisor-only 门 #6/#7 留 M0→M1 人工渗透；egress 门 #3 待 F.9。
- **理由**：(1) F.4–F.7 收尾时并无 sandbox-Docker 集成 harness，假桩单测先保证逻辑正确、PR 不被 Docker 慢拉卡住。(2) 集成门集中一个 PR 落地，harness（建镜像 / 建网络 / 扫容器）只写一份。(3) GitHub Actions 标准 runner 自带 Docker，runc 可跑；§ 1.3 已明确 runc 覆盖 #1/#2/#5 隔离语义，gVisor 只为 #6/#7 的 syscall 拦截 / 逃逸面服务。
- **代价**：F.4–F.7 合并时这批门有一个未被自动化守住的窗口期；F.8 收口。F.8 实测即发现并修正了 F.7 强制 destroy 的序问题（见 Mini-ADR F-8 补充）—— 印证"假桩单测漏真实路径"的风险。

### F-11：集成测试用进程内 `SandboxSupervisor` + 真实 `CliDockerClient`，不起 HTTP

- **替代**：测试里 `uvicorn` 拉起完整 supervisor FastAPI app，走 HTTP。
- **选择**：直接 `SandboxSupervisor(docker=CliDockerClient(), store=<内存>, ...)` 进程内构造，真实 Docker，跳过 HTTP 层。
- **理由**：(1) HTTP 路由层已被 `TestClient` smoke 测覆盖（测试矩阵 #40 系列）；集成测试该验的是 Docker 真实路径。(2) 进程内构造更快、可直接断言 `SandboxRecord` 状态。(3) store 用内存假实现 —— F.8 验的是 Docker 隔离，不是 DB。
- **代价**：HTTP ↔ supervisor 端到端串联未被集成测试覆盖；可接受 —— 该串联是窄序列化层，单测 + smoke 已够。

### F-12：F.8 跑在既有非 gating 的 `Test (integration)` job，镜像在 fixture 内 build

- **替代**：给 F.8 单开一个 CI job + workflow 内 `docker build` 步骤。
- **选择**：F.8 测试标 `@pytest.mark.integration`，由既有 `Test (integration)` job（`continue-on-error: true`）自然收集；sandbox 镜像由 pytest session fixture `docker build`，缺 Docker → `pytest.skip`。
- **理由**：(1) 该 job 已 `continue-on-error` —— 镜像 build / 拉取抖动不卡 PR（与 ci.yml 既有注释口径一致）。(2) fixture 内 build 让测试自包含，本地 `pytest -m integration` 与 CI 同路径。(3) workflow 几乎不动，零新增 job。
- **代价**：F.8 验收门是非 gating 的 —— 失败不挡合并、需人看 CI。M0 可接受（M1 自托管 runner + 暖 Docker 缓存后再升 gating）。

### F-13：沙盒镜像基础镜像 = `python:3.12-alpine`，非 slim / 非 distroless

- **替代**：`python:3.12-slim`（现状，203MB）；自建多阶段 distroless（~55MB 且无 shell）。
- **选择**：`python:3.12-alpine`（~50MB）。
- **理由**：(1) 调研 deer-flow / hermes-agent —— 两者都保留肥镜像 + shell，**因为要让 agent 在沙盒内 `pip/npm install` 装包**；Expert Work Mini-ADR F-2 是无 egress、pip 已卸载、纯 stdlib 执行，那个理由对我们不成立。(2) 不装包 → musl libc 兼容性非问题（musl 差异只在第三方 C 扩展编译期显现）；alpine 官方 python 镜像自带完整 musl CPython。(3) distroless 官方镜像锁 Python 3.11（我们要 3.12），自建多阶段要手工追 CPython 的 `.so` 依赖闭包并长期自维护手搓镜像 —— 代价 > 收益。
- **代价**：alpine 仍带 busybox（shell + applets），非 distroless 的彻底无 shell。在 gVisor + `--cap-drop ALL` + `--read-only` + non-root 之下 shell 的攻击价值很低，M0 可接受；彻底无 shell 推 M1 加固再评估。验收护栏：测试矩阵 #59（stdlib C 扩展模块在 musl CPython 上可 import）。

### F-14：M0 sandbox egress 强制 = Docker `--internal` 网络，非 sandbox-side iptables

- **替代 A**：subsystem 21 § 5.1 字面方案 —— sandbox `iptables OUTPUT` chain（DROP 默认 + allowlist ACCEPT）。**替代 B**：宿主侧 `DOCKER-USER` 链 iptables allowlist。
- **选择**：`expert-work-sandbox-egress` 创建为 Docker `--internal` 网络（`docker network create --internal`）；credential-proxy **双归属**（internal 网 + 一个有出网能力的网络）。
- **理由**：(1) 替代 A **不可行** —— Mini-ADR F-5 强制 `--cap-drop=ALL`，沙盒容器无 `CAP_NET_ADMIN`，无法给自己设 iptables；要在沙盒 netns 设规则得上 OCI prestart hook（Docker 不易挂）或宿主特权进程。(2) `--internal` 网络 Docker 不装 NAT / 默认路由 —— 沙盒结构性够不到公网、`169.254.169.254` / `100.100.100.200` 等云元数据、宿主、其它 docker 网段，**验收门 #3 直接成立、零 iptables**。(3) Docker 原生、CI 可测（`docker network create --internal` 即可）、不与 Docker 自管的 iptables 链冲突。
- **代价**：(1) 同一 internal 网上的沙盒可互通（sandbox A ↔ sandbox B）—— M0 单 agent、低并发（Mini-ADR F-4）下风险低，**M0 接受**；M1 warm pool 时一沙盒一网或加一条 `DOCKER-USER` 规则。(2) 偏离 subsystem 21 字面（iptables OUTPUT）—— 该文档 M0 段设计早于本加固清单，本 ADR 记录校正；subsystem 21 的 iptables / Envoy 透明代理归 M1。(3) subsystem 21 的受控 unbound DNS：`--internal` 下沙盒无对外路由，DNS 解析了也连不上 —— 验收门 #3 不需要 unbound，它是 DNS 渗透加固，归 **M1**。

### F-15：F.10 给 credential-proxy 写正式多阶段 uv 构建 Dockerfile，确立 Expert Work 服务容器化 pattern

- **替代**：compose 服务用基础镜像 + bind-mount 源码 + `command: uv run ...`（dev-mode，不写 Dockerfile）。
- **选择**：F.10 给 credential-proxy 写正式多阶段 uv 构建 Dockerfile（builder 阶段 `uv sync` workspace → runtime 阶段 copy `.venv`）；credential-proxy 作为 pilot 确立"Expert Work 服务容器化"pattern，其余 3 服务复用（ITERATION-PLAN I.1）。
- **理由**：(1) dev-mode 会返工 —— F.11 端到端测试本就需要真 proxy 容器，dev-mode 下 harness 还得重复 bind-mount + `uv run`，且 compose 的 dev-mode stanza 将来"容器化"任务原样推翻重写。(2) credential-proxy 是 4 个 Expert Work 服务里最简单的（单 aiohttp app），是踩 uv workspace 单体打包坑的低风险 pilot。(3) 多阶段 uv 构建是 uv 官方有文档的成熟套路，非重型架构决策；F.9 harness 已证明 CI 能 build+run 镜像、可验证。
- **代价**：F.10 PR 比 dev-mode 版略大（含一个 Dockerfile）。可接受 —— 一次做对、无返工。其余 3 个服务（control-plane / orchestrator / sandbox-supervisor）复用此 pattern 容器化 + 全栈 compose，归 ITERATION-PLAN **Stream I.1**（M0 部署与发布闭环）。

---

## 4. 接口

### 4.1 Sandbox Supervisor HTTP API（内网，mTLS）

```
POST /v1/sandboxes:acquire     Body: AcquireRequest     → AcquireResponse
POST /v1/sandboxes/{id}:release Body: {}                → 204
POST /v1/sandboxes/{id}:destroy Body: {"reason": str}   → 204
GET  /v1/health                                          → liveness + docker daemon 状态
```

M0 `AcquireRequest` 为 subsystem 14 § 3.3 的子集（无 `isolation_level` 分支、无 `purpose`）：

```python
class AcquireRequest(BaseModel):
    tenant_id: UUID
    image_ref: str
    thread_id: str
    cpu: float = 1.0
    memory_mb: int = 512
    pids_limit: int = 128
    timeout_s: int = 30

class AcquireResponse(BaseModel):
    sandbox_id: UUID
    container_id: str
    cold_start: bool = True          # M0 恒 True（无 warm pool）
    acquired_at: datetime
```

所有调用带 `X-Expert-Work-Tenant`，由 C.2 mTLS（XFCC）认证调用方 = control-plane。

### 4.2 `runner.py` ↔ Supervisor 协议（容器内 PID 1）

stdin/stdout 行分隔 JSON（容器无网络给 runner，只有 docker attach 的 stdio）：

```
→ {"code": "<python source>", "timeout_s": 30}
← {"stdout": "...", "stderr": "...", "exit_code": 0, "timed_out": false}
```

### 4.3 `exec_python` 工具（orchestrator `Tool` Protocol）

```python
# services/orchestrator/src/orchestrator/tools/sandbox.py
class ExecPythonTool:                       # 实现 Tool Protocol
    async def call(self, args: dict, *, ctx: ToolContext) -> ToolResult:
        """args = {"code": str, "timeout_s": int}。
        acquire → exec → finally：正常 / 错误路径 release，取消路径
        （asyncio.CancelledError）destroy(reason="cancelled")。"""
```

注册：`BuiltinToolSpec(type="builtin", name="exec_python")`（Stream E 判别联合）。

### 4.4 Credential Proxy HTTP 契约

```
POST /forward
  X-Expert-Work-Tenant / X-Expert-Work-Agent / X-Expert-Work-Agent-Version / X-Expert-Work-Session
  X-Expert-Work-Secret-Ref: <ref>          要注入的 secret 引用
  X-Expert-Work-Upstream: <https url>       真实上游目标
  Body: <原始请求体>
→ 上游响应（X-Expert-Work-Secret-Ref 从响应中剥离）
```

管理 API：`POST /admin/allowlist` / `DELETE /admin/allowlist/...` / `POST /admin/cache/invalidate` / `GET /admin/health`。

> **勘误（B-31 ②，2026-08-31）**：本行原写「仅 mTLS SAN=control-plane 可达」。
> 该限制**从未实现** —— Service 是普通 ClusterIP，仓库里也没有任何 NetworkPolicy。
> 这句声明本身是这个洞坐了这么久没被查出来的原因（写下的控制不等于生效的控制）。
> 现在三个写入 / 失效路由要 `Authorization: Bearer $EXPERT_WORK_CRED_PROXY_ADMIN_TOKEN`，
> **未配置该 token 时是 503 而不是放行**；`GET /admin/health` 保持开放供 kubelet 探针使用。
> mTLS 若要做，是这道 token 闸之上的加固，不是它的替代。

### 4.5 SecretStore `aliyun_kms` —— 实现既有 Protocol

无新接口 —— `AliyunKmsSecretStore` 实现 `packages/expert-work-runtime/.../secret_store/base.py` 的 `SecretStore` Protocol（`get` / `put` / `list_versions`）；`make_secret_store` factory 的 `"aliyun_kms"` 分支从 `raise NotImplementedError` 改为返回实例。

### 4.6 Migration — `sandbox_instance` + `secret_allowlist` + `credential_proxy_audit`

新 migration（接 0011 之后，编号待落地时确认）：建 subsystem 14 § 3.2 的 `sandbox_instance`（M0 写全字段）+ subsystem 11 § 3.1 的 `secret_allowlist` / `credential_proxy_audit`。`sandbox_image` 表 M0 不建（无 layer cache，M1-A 再建）。

### 4.7 新 AuditAction（Stream F 新增）

`packages/expert-work-protocol/.../audit.py` `AuditAction` 追加：`SANDBOX_ACQUIRED` / `SANDBOX_DESTROYED` / `SANDBOX_QUOTA_DENIED` / `SECRET_INJECTED` / `SECRET_INJECT_DENIED`。同步更新 `docs/architecture/subsystems/17-audit-log.md` action 目录。

---

## 5. 测试矩阵

> 接续 Stream E 测试矩阵编号（E 收于 #39），Stream F 用 #40 起。

| # | 维度 | PR | 类型 | 关键 case |
|---|------|----|----|-----------|
| 40 | Supervisor acquire/release | F.1 | unit | mock docker client → `acquire` 返回 `sandbox_id`；`release` 调 `docker rm -f`；状态机 `CREATING→IN_USE→DESTROYED` |
| 41 | Supervisor 配额拒绝 | F.1 | unit | tenant 已用 = `max_sandboxes` → `acquire` 返回 429 + `SANDBOX_QUOTA_DENIED` audit |
| 42 | Supervisor TTL reaper | F.1 | unit | `IN_USE` 且超 `timeout_s+30s` 的孤儿 → reaper 周期内被 destroy |
| 43 | 镜像加固 flag | F.2/F.3 | unit | `SandboxRuntimeProvider` 注入的 `docker run` 参数含全 6 条加固 flag；dev=runc / prod=runsc |
| 44 | runner.py 协议 | F.2 | unit | stdin `{code}` → stdout `{stdout,stderr,exit_code}`；`timeout_s` 超时 → `timed_out=true` |
| 45 | exec_python 跑通 | F.4 | integration | `code="print(2+2)"` → `ToolResult.content` 含 `4`、`meta.exit_code=0`（Linux CI，runc） |
| 46 | exec_python 输出截断 | F.4 | unit | `code` 打印 50k 字符 → stdout 截到 20k + `meta.truncated=true` |
| 47 | sandbox_audit 拦截 | F.4 | unit | `code="import os; os.system('rm -rf /')"` → middleware 拒绝 + audit；`code="print(1)"` 通过 |
| 48 | 文件 / 进程隔离 | F.4 | integration | 验收门 #1 / #2（Linux CI） |
| 49 | 网络隔离 | F.9 | integration | 验收门 #3 —— `--internal` 网络下 sandbox 连 `169.254.169.254` 等 refused；连同 internal 网 stub proxy 通 |
| 50 | secret 不可见 | F.5 | integration | 验收门 #4 —— sandbox `env` / `/run/secrets` 无凭证 |
| 51 | Credential Proxy 注入 | F.5 | unit | `X-Expert-Work-Secret-Ref` 命中 allowlist → 注入 `Authorization`；越权 ref → 403 + audit |
| 52 | Proxy LRU 缓存 | F.5 | unit | 同 `(tenant,ref)` 二次请求 → 命中 LRU、不再调 SecretStore；TTL 过期 → 重拉 |
| 53 | Proxy 审计无明文 | F.5 | unit | `credential_proxy_audit` 行含 ref + host + status，**断言不含 secret 明文** |
| 54 | AliyunKmsSecretStore | F.6 | unit | mock 阿里云 SDK → `get` 返回值；`SecretNotFoundError`；短 TTL 缓存命中 / 过期 |
| 55 | make_secret_store 接通 | F.6 | unit | `make_secret_store("aliyun_kms")` 返回实例（不再 `NotImplementedError`） |
| 56 | fork bomb PID limit | F.4 | integration | 验收门 #5（Linux CI） |
| 57 | 取消即杀 | F.7 | integration | 验收门 #8 —— long-running `exec_python` → cancel → ≤1s `DESTROYED` |
| 58 | 取消 finally 释放 | F.7 | unit | `exec_python` 异常 / 取消路径均走到 `supervisor.destroy`（无泄漏容器） |
| 59 | stdlib C 扩展可用 | F.8 | integration | Mini-ADR F-13 —— alpine/musl CPython 可 import `ssl`/`sqlite3`/`ctypes`/`lzma` 等 C 扩展 stdlib（切基础镜像护栏） |
| 60 | egress 端到端 | I.1 | integration | `exec_python` → sandbox →（仅）真 credential-proxy → mock upstream 全链路通（需全栈，原属 F.11 → 移入 I.1）|

> 验收门 #6（timing/side-channel）、#7（CVE-2019-5736 PoC）需真实 runsc，不进 CI 自动化 —— 归 M0→M1 Gate 沙盒渗透测试，§ 1.3 已注明。**Stream K.K5 把这两条锁进 [ITERATION-PLAN.md § M0→M1 Gate Exit Criteria](../ITERATION-PLAN.md) 必跑通条款**（"gVisor 7/7 staging Linux 跑通"），软推迟不再被接受。
>
> **F.8 收口**：测试矩阵 #45 / #48 / #50 / #56 / #57 的 integration 用例由收尾 PR **F.8** 用真实 Docker（runc）集成 harness 实装 —— F.4–F.7 当时只落假桩单测，详见 Mini-ADR F-10。#49（egress 隔离）依赖的 iptables allowlist M0 未实现，待 **F.9**。

---

## 6. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| gVisor macOS 不可用，dev 跑不了真隔离 | `SandboxRuntimeProvider` 抽象：dev=runc / prod=runsc；CI 在 Linux runner 跑 runsc 用例（验收门 #6/#7）；dev 只验功能不验隔离强度 |
| `docker run` 冷启动 1-3s 拖慢 `exec_python` | M0 接受（subsystem 14 § 9）；M1-A warm pool 降到 P95 < 500ms；`exec_python` 工具向 LLM 返回的 `meta` 不暴露延迟，不影响推理 |
| `sandbox_audit` 黑名单绕过（LLM 用 `getattr` / `__import__` 混淆） | M0 黑名单是**纵深防御的一层**，不是唯一防线 —— 真正的边界是 gVisor + cap-drop + read-only + 无网络出口；`sandbox_audit` 误放行也跑不出沙盒 |
| Credential Proxy 死代码风险（无消费者） | Mini-ADR F-2 让 sandbox 出站强制经 proxy → proxy 有真实 M0 消费者；验收门 #3/#4 覆盖 |
| sandbox 容器泄漏（崩溃没 release） | supervisor TTL reaper 每 10s 扫 `IN_USE` 孤儿强杀（测试 #42）；`exec_python` 工具 `finally` 块兜底（测试 #58） |
| 阿里云 KMS 不可达 → secret 拉取失败 | proxy 进程内 LRU 内仍有效的 secret 继续可用；control-plane 启动加载的 LLM key 已在内存；KMS 故障只影响**新** ref 首次拉取 → 告警 + degraded |
| 取消信号没在 1s 内杀干净 | `docker kill` 直接 SIGKILL 不等 graceful（Mini-ADR F-8）；验收门 #8 量化守门 |
| 镜像供应链污染 | M0 本地 build + 私有使用，无外部 pull；cosign 签名验证推 M1-A（subsystem 18） |
| 两个新服务的部署 / mTLS 配置遗漏 | 复用 C.2 既有 mTLS（XFCC）；docker-compose 同机编排，infra/docker-compose.yml 加两 service |

---

## 7. 里程碑 / PR 切分

一 PR 一子项（沿用 Stream E 节奏），bottom-up：

```
F.2  feat(f-2): sandbox 镜像 + runner.py
        - infra/sandbox-image/Dockerfile（python:3.12-slim + 加固）
        - runner.py（PID 1，stdin/stdout JSON 协议）
        - unit：runner 协议 / 超时（测试 #44）

F.3  feat(f-3): SandboxRuntimeProvider + gVisor 启动路径
        - SandboxRuntimeProvider 抽象（dev runc / prod runsc）
        - docker run 加固 flag 固化
        - unit：加固 flag 断言（测试 #43）

F.1  feat(f-1): sandbox-supervisor 服务
        - services/sandbox-supervisor/（acquire/release/destroy HTTP API）
        - migration：sandbox_instance 表
        - 冷启动状态机 + 配额计数 + TTL reaper
        - unit：测试 #40 #41 #42

F.6  feat(f-6): SecretStore aliyun_kms 后端
        - AliyunKmsSecretStore adapter + 短 TTL 缓存
        - make_secret_store 接通 aliyun_kms 分支
        - unit：测试 #54 #55

F.5  feat(f-5): credential-proxy 服务
        - services/credential-proxy/（aiohttp /forward + allowlist + LRU）
        - migration：secret_allowlist + credential_proxy_audit 表
        - sandbox-egress docker 网络 + iptables allowlist
        - unit：测试 #51 #52 #53；integration：#49 #50

F.4  feat(f-4): exec_python 工具接入 orchestrator
        - orchestrator/tools/sandbox.py（ExecPythonTool）
        - 注册 BuiltinToolSpec；sandbox_audit 中间件装进 before_tool_dispatch 链
        - 输出截断（复用 tools.truncation helper）
        - 新 AuditAction（SANDBOX_* / SECRET_*）
        - unit：测试 #46 #47；integration：#45 #48 #56

F.7  feat(f-7): cancellation → sandbox kill
        - exec_python finally 分流：取消 → destroy(reason=cancelled)，正常/错误 → release
        - supervisor destroy = docker kill（SIGKILL）
        - unit：测试 #58

F.8  feat(f-8): sandbox Docker 集成测试 harness
        - services/sandbox-supervisor/tests/test_supervisor_integration.py
        - session fixture：docker build 镜像 + 建 egress 网络 + skip-if-no-docker
        - integration：测试矩阵 #45 #48 #50 #56 #57（runc，验收门 #1/#2/#4/#5/#8）
        - 修正 supervisor 强制 destroy 序（先 docker rm 再 link.close，见 Mini-ADR F-8 补充）
        - 跑在既有非 gating Test (integration) job（Mini-ADR F-10/F-11/F-12）

F.9  feat(f-9): sandbox egress 网络隔离（--internal 网络）
        - expert-work-sandbox-egress 改 Docker --internal 网络（Mini-ADR F-14）
        - harness：建 --internal 网 + stub proxy 容器；新增测试矩阵 #49
        - 不碰 compose、不写 iptables；关闭验收门 #3

F.10 feat(f-10): credential-proxy 容器化 + 入 docker-compose
        - services/credential-proxy/Dockerfile：多阶段 uv 构建（确立 Expert Work 服务容器化 pattern）
        - infra/docker-compose.yml 加 expert-work-sandbox-egress(internal)/egress 双网络
        - credential-proxy 服务双归属（Mini-ADR F-15）

F.11 feat(f-11): control-plane 接 ToolEnv.supervisor_client
        - control-plane settings 加 sandbox-supervisor base URL
        - build_tool_env 注入 HTTPSupervisorClient → manifest 声明 exec_python 可端到端用
        - #60 全栈 egress e2e 移入 ITERATION-PLAN I.1（需真 proxy + postgres + 迁移全栈）
```

> **PR 顺序说明**：F.2/F.3 先于 F.1 —— supervisor `acquire` 依赖镜像 + runtime provider 存在。F.6 先于 F.5 —— proxy 取 secret 依赖 `aliyun_kms` 后端。F.4 在 F.1+F.5 之后 —— `exec_python` 同时依赖 supervisor 和（经 sandbox 出网时）proxy。F.7 接 cancellation；F.8 把 § 1.3 的 runc 验收门补成自动化集成 harness。F.9 → F.10 → F.11 是 egress 隔离链路：F.9 把 `expert-work-sandbox-egress` 改 `--internal`（stub proxy 验 #49），F.10 把真 proxy 容器化 + 接入 compose，F.11 把 supervisor client 接进 control-plane 生产装配。全栈 egress e2e（#60）随 I.1 的 `docker compose up` 一并做。

---

## 8. 横切依赖回看（自下而上验证）

| 依赖 | 来源 Stream | F 如何对接 |
|------|------------|-----------|
| `CancellationToken`（协作式、config 通道）| E.15 | F.7 `exec_python` 用 `run_cancellable` 包；token 取消 → `destroy` |
| `sandbox_audit_middleware` | E.10（已实现未装配）| F.4 装进 `before_tool_dispatch` 链，首次真跑 |
| `Tool` Protocol + 工具注册表 + `BuiltinToolSpec` 判别联合 | E.6 / E.7-E.9 / E-14 | F.4 `exec_python` 实现 `Tool`、注册为 builtin |
| `tools.truncation` 共享 helper | E-10 | F.4 输出截断复用（Mini-ADR F-9） |
| `SecretStore` 抽象 + `make_secret_store` factory | F.6 抽象层（Stream E 已落）| F.6 补 `aliyun_kms` 后端；F.5 proxy 依赖之 |
| mTLS 服务认证（XFCC header）| C.2 | supervisor / proxy 的内网 API 复用 |
| `tenant_quota.max_sandboxes` | C.5 quota | F.1 `acquire` 配额计数校验 |
| `AuditLogger` / `AuditEntry` / `AuditAction` | A.4 / D.2 | F 新增 5 个 AuditAction，写审计同既有路径 |
| `infra/docker-compose.yml` | A 基建 | 加 `sandbox-supervisor` / `credential-proxy` 两 service |

**验证**：F 不引入新的架构槽 —— sandbox-supervisor / credential-proxy 是 subsystem 14 / 11 早已定义的组件；`exec_python` 落在 E 已建的"工具体系"槽；F.6 落在 Stream E 已建的 `SecretStore` 抽象槽。

---

## 9. 与 ITERATION-PLAN 对照

| ITERATION-PLAN 条目 | 本文档落点 |
|---------------------|-----------|
| F.1 Sandbox Supervisor 服务 | § 2.1 / § 2.2 / § 4.1；Mini-ADR F-3 F-4 |
| F.2 单 Python 镜像 | § 2.3；Mini-ADR F-5 |
| F.3 Docker + gVisor (runsc) | § 2.3；Mini-ADR F-3（runtime provider）|
| F.4 `exec_python` 工具接入 | § 2.4；Mini-ADR F-1 F-9 |
| F.5 Credential Proxy aiohttp 自研版 | § 2.5；Mini-ADR F-2 F-6 F-7 |
| F.6 SecretStore + 阿里云 KMS | § 2.6；Mini-ADR F-7 |
| F.7 请求取消的 sandbox kill | § 2.7；Mini-ADR F-8 |
| Stream F Verification 7+1 条 | § 1.3 验收门；§ 5 测试矩阵 #48-#50 #56-#57 |
| `exec_python` 工具名（vs deer-flow bash/ls/read_file）| Mini-ADR F-1 新增澄清 —— ITERATION-PLAN 未单列工具面取舍 |
| sandbox 出站网络策略 | Mini-ADR F-2 新增 —— ITERATION-PLAN 未明确 M0 sandbox 是否有网络 |
| F.8 验收门集成 harness | § 1.1；§ 5；Mini-ADR F-10 F-11 F-12 —— ITERATION-PLAN 未列，作为 Stream F 设计细化新增 |
| F.9 / F.10 / F.11 egress 网络隔离链路 | § 1.1；§ 5 #49 #60；Mini-ADR F-14 F-15 —— ITERATION-PLAN 未列，作为 Stream F 设计细化新增 |
