# Stream I — 部署与发布闭环（设计先行）

> 落实 [docs/ITERATION-PLAN.md](../ITERATION-PLAN.md) § Stream I。
> 承接 Phase 0.3 已建的 baseline CI/CD + 三环境配置框架，把它生产化。
> 执行 [architecture/07-INFRASTRUCTURE-GAPS](../architecture/07-INFRASTRUCTURE-GAPS.md) §10 的 M0 子集。
>
> **覆盖范围**：§ 1 – § 5 = I.1（服务容器化 + 全栈 compose，已落地）；§ 6 = I.2（发布策略）、§ 7 = I.3（回滚 + DB 兼容）、§ 8 = I.4（三环境部署文档）。

设计先行规则（[memory:feedback_design_first_iteration.md](../../.claude/projects/-Users-mac-src-github-jone-qian-expert-work/memory/feedback_design_first_iteration.md)）：
所有架构 / 接口 / Mini-ADR 必须在编码前锁定，I.1 的 PR 仅执行本文档。

> **背景**：Stream F.10 已给 credential-proxy 写了正式多阶段 uv 构建 Dockerfile，作为 pilot 确立"Expert Work 服务容器化"pattern（Mini-ADR F-15）。I.1 把该 pattern 复用到其余服务，并把 M0 完整应用栈拼进一条 `docker compose up`。

---

## 1. 范围 & 边界

### 1.1 In-scope（I.1）

| 子项 | 实现内容 | 关联 |
|------|---------|------|
| **I.1a 服务容器化 + 全栈 compose** | 给 control-plane、sandbox-supervisor 各写多阶段 uv 构建 Dockerfile（复用 F.10 pattern）；`infra/docker-compose.yml` 加 `migrate`（一次性 `alembic upgrade head`）、`control-plane`、`sandbox-supervisor` 三个服务 + `full` profile —— `docker compose --profile full up` 起 M0 完整应用栈。 | Mini-ADR I-1 I-2 I-3 |
| **I.1b 全栈 egress 端到端测试** | 测试矩阵 #60：`exec_python` → sandbox →（仅）真 credential-proxy → mock upstream 全链路通。原属 Stream F.11，因需 proxy + postgres + 迁移全栈而移入 I.1（STREAM-F-DESIGN § 1.1 F.11）。 | 测试矩阵 #60 |

> I.1a / I.1b 拆两个 PR：I.1b 的 e2e 依赖 I.1a 的全栈 compose 就位。

### 1.2 Out-of-scope（明确推迟）

| 推迟项 | 落地 | 备注 |
|-------|------|------|
| orchestrator 拆独立服务 + 独立镜像 | M1（若 in-process 单体撑不住再拆）| Mini-ADR I-1 —— M0 orchestrator 是库，随 control-plane 镜像发布 |
| sandbox-supervisor 真 DinD / K8s Pod + RuntimeClass=gvisor | M3 | Mini-ADR I-2 —— M0 用 docker-out-of-docker（挂宿主 socket）|
| audit-backup-worker / retention-cleanup-job 容器化 | I.1 之外（按需）| 二者是 cron 式 job，非 M0 在线栈常驻服务，不在"全栈 compose up"目标内 |
| 蓝绿 + 金丝雀发布脚本 | I.2 | P0 #32 |
| 一键回滚 + DB 兼容 | I.3 | P0 #33 |
| 三环境（dev/staging/prod）部署文档 | I.4 | — |
| 镜像 build cache + 私有 registry push、Trivy/cosign CI gate | M1-A | M0 本地 `docker build` |

### 1.3 验收（I.1 Exit）

1. `docker compose --profile full up` 在干净环境把 M0 应用栈全部拉起，所有服务 healthcheck 转 healthy。
2. control-plane 镜像、sandbox-supervisor 镜像均多阶段构建、non-root、只携带 `.venv`。
3. 测试矩阵 #60 全栈 egress e2e 在 `Test (integration)` job 绿。
4. ITERATION-PLAN I.1 checklist 全勾，文字与实现一致。

---

## 2. 架构

### 2.1 M0 容器镜像清单

M0 在线栈 = **3 个 Expert Work 服务镜像** + 数据层（用社区镜像，不自建）：

| 镜像 | 服务 | 入口 | 端口 | 状态 |
|------|------|------|------|------|
| `expert-work-control-plane` | control-plane（FastAPI）| `uvicorn control_plane.main:app` | 8000 | I.1 新建 |
| `expert-work-sandbox-supervisor` | sandbox-supervisor（FastAPI）| `uvicorn sandbox_supervisor.main:app` | 8000 | I.1 新建 |
| `expert-work-credential-proxy` | credential-proxy（aiohttp）| `python -m credential_proxy.main` | 8080 | F.10 已建 |

> **orchestrator 不在表内** —— 它是纯库（`agent_factory` / `graph_builder` / `runner` / `tools`，无 `main` / `app`），control-plane 把它当 workspace 依赖装进自己的镜像里 in-process 跑（STREAM-E-DESIGN § 2.6）。详见 Mini-ADR I-1。
>
> `expert-work-sandbox`（沙盒执行镜像，`infra/sandbox-image/`）不是 compose 服务 —— 它由 sandbox-supervisor 在运行期 `docker run` 启。全栈 `compose up` 前需在宿主预构建（见 § 2.4）。

### 2.2 容器化 pattern（复用 F.10）

每个 Expert Work 服务镜像统一两阶段：

- **builder** `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`：`COPY pyproject.toml uv.lock packages/ services/` → `uv sync --frozen --no-dev --no-editable --package <pkg>` —— 整个 workspace 在场供 uv 解析，但 `--package` 只构建目标服务 + 其 workspace 依赖，装成非可编辑 wheel 进自包含 `.venv`。
- **runtime** `python:3.12-slim-bookworm`：non-root 服务账户，`COPY --from=builder /app/.venv`，`ENTRYPOINT` 直接起服务。

构建均从仓库根：`docker build -f services/<svc>/Dockerfile -t expert-work-<svc> .`

sandbox-supervisor 比 pilot 多一步：它要 shell out `docker` CLI（`CliDockerClient`），故 runtime 阶段从 `docker:cli` 镜像 `COPY` 一个静态 `docker` 客户端二进制 —— 见 Mini-ADR I-2。

### 2.3 全栈 compose 拓扑

```
                       expert-work-postgres ──┐ (5432 direct)
                              │         │
                       expert-work-pgbouncer  │ (6432 txn pool)
                              │         │
   ┌──────────────────────────┼─────────┼────────────────┐
   │                          │         │                │
expert-work-control-plane    expert-work-redis  expert-work-sandbox-     expert-work-credential-
  :8000 (host)         (quota)      supervisor :8000    proxy :8080
   │  exec_python tool                │ docker.sock      │
   │  ──HTTP──────────────────────────┘ (DooD)           │
   │                                   │ docker run      │
   │                                   ▼                 │
   │                            expert-work-sandbox 容器 ──────┘
   │                            (join expert-work-sandbox-egress, --internal)
   └─ migrate (一次性, alembic upgrade head; 上述服务 depends_on 它完成)
```

- **数据层**（postgres / pgbouncer / redis / minio）：无 profile，恒起。
- **`full` profile**：`migrate`、`control-plane`、`sandbox-supervisor`、`credential-proxy` 同入 `full`（credential-proxy 同时保留既有 `sandbox` profile）。`docker compose --profile full up` = M0 完整应用栈。
- **migrate**：一次性服务，跑 `alembic upgrade head` 后退出；control-plane / sandbox-supervisor / credential-proxy 经 `depends_on: { migrate: { condition: service_completed_successfully } }` 等它完成 —— schema 先于服务就绪，无 race。
- **control-plane**：经 pgbouncer（6432）连库；redis 作 quota/限流后端；`EXPERT_WORK_SANDBOX_SUPERVISOR_URL=http://sandbox-supervisor:8000` 接 `exec_python`；端口 8000 映射宿主。
- **sandbox-supervisor**：直连 postgres（5432，settings 默认即如此）；挂 `/var/run/docker.sock`；沙盒由它 `docker run` 启并 join `expert-work-sandbox-egress`。
- **网络**：`expert-work-sandbox-egress`（`--internal`，F.9 Mini-ADR F-14 已建）只连 sandbox ↔ credential-proxy；其余服务走 `default` 网。credential-proxy 双归属（F.10 已建）。

详见 Mini-ADR I-3。

### 2.4 `expert-work-sandbox` 镜像预构建

sandbox-supervisor 运行期才 `docker run expert-work-sandbox:dev`，compose 不会替它构建。全栈 `compose up` 前需：

```
docker build -f infra/sandbox-image/Dockerfile -t expert-work-sandbox:dev infra/sandbox-image
```

在 `infra/README.md` 与 compose 服务注释中文档化此前置步骤（M0 无 Makefile，不为单条命令引入构建工具）。

### 2.5 失败模式

| 失败场景 | 处理 |
|---------|------|
| migrate 失败（schema 迁移报错）| `migrate` 退出码非 0 → `depends_on` 的 `service_completed_successfully` 不满足 → 依赖服务不启动，`compose up` 显式失败，不会出现"服务起来了但表不存在" |
| 宿主未预构建 `expert-work-sandbox:dev` | sandbox-supervisor 自身能起（healthcheck 看 8000 监听）；首次 `exec_python` 时 supervisor `docker run` 报 image not found → `exec_python` 工具返回明确错误。文档化预构建步骤兜底 |
| 宿主 `docker` 组 GID 与容器内不匹配 | sandbox-supervisor compose 服务用 `user: root` 跑 —— 挂 socket 已等价宿主 root，容器内 user 不构成实质差异（Mini-ADR I-2）；规避 GID 不可移植 |
| control-plane 先于 sandbox-supervisor 就绪 | `exec_python` 才需要 supervisor；control-plane 启动不 `depends_on` supervisor 的 healthy（仅松依赖），首个 run 触发时若 supervisor 未就绪则该 HTTP 调用失败并返回工具错误，不影响 control-plane 自身存活 |

---

## 3. Mini-ADR

### Mini-ADR I-1：M0 容器镜像清单 = control-plane + sandbox-supervisor + credential-proxy，orchestrator 不独立成镜像

- **背景**：ITERATION-PLAN I.1 原文写"4 个 Expert Work 服务（control-plane / orchestrator / credential-proxy / sandbox-supervisor）"多阶段构建。
- **事实**：`services/orchestrator/` 是纯库 —— 只有 `agent_factory` / `graph_builder` / `runner` / `llm` / `tools` 等模块，**无 `main.py` / `app.py` / server 入口**。STREAM-E-DESIGN § 2.6 明确 M0 是 in-process 单体：control-plane 把 `expert-work-orchestrator` 作为 workspace 依赖，agent graph 以后台 `asyncio.Task` 在 control-plane 进程内执行，不是独立服务。
- **决策**：M0 建 **2 个新镜像**（control-plane、sandbox-supervisor），credential-proxy 镜像由 F.10 已建。orchestrator 代码随 control-plane 镜像发布，**不独立容器化**。
- **代价**：ITERATION-PLAN I.1 checklist 文字与实际偏离 → 同步修订（I.1 子项之一）。orchestrator 拆独立服务 + 独立镜像推 M1 —— 仅当 in-process 单体撑不住（如需独立扩缩容 orchestrator）才做；M0 不预先拆。

### Mini-ADR I-2：sandbox-supervisor 容器 = docker-out-of-docker（挂宿主 docker.sock）

- **背景**：sandbox-supervisor 的 `CliDockerClient` shell out `docker run -i` 启沙盒、`docker rm` 回收。supervisor 自身容器化后，需要一个 Docker daemon 可达。
- **选项**：
  - **(a) docker-out-of-docker (DooD)** —— 挂宿主 `/var/run/docker.sock`，supervisor 容器内的 `docker` CLI 直连宿主 daemon。沙盒成为 supervisor 容器的**兄弟容器**（同宿主 daemon），join `expert-work-sandbox-egress` 仍可达 credential-proxy（同 daemon、同网络）。
  - **(b) DinD** —— `docker:dind` sidecar 跑嵌套 daemon。
  - **(c) supervisor 不容器化** —— 跑宿主。
- **决策**：**(a) DooD**。supervisor 镜像 runtime 阶段从 `docker:<ver>-cli` 镜像 `COPY` 静态 `docker` 客户端二进制（supervisor 只需 client，不需 daemon）。compose 服务挂 `/var/run/docker.sock:/var/run/docker.sock`。
- **理由**：(1) (b) DinD 要 `--privileged` 跑嵌套 daemon、叠加存储驱动开销大；且沙盒 + `expert-work-sandbox-egress` 在嵌套 daemon 内、credential-proxy 在外层 daemon → 跨 daemon 网络打通复杂。(2) (c) 破坏"一条 `compose up` 起全栈"的 I.1 目标，且 dev / dogfood 单机部署不一致。(3) DooD 是 CI runner / Jenkins agent 启容器的标准做法，沙盒隔离仍由 `docker run` 的加固 flag（`--cap-drop=ALL` / `--read-only` / gVisor）保证 —— supervisor 容器是否 DooD 不削弱沙盒隔离。
- **代价**：挂 docker.sock = supervisor 容器**等价宿主 root**。这是 M0 本地 dev / 单机 dogfood 的**已知信任边界**，非新增暴露 —— supervisor 的职责本就是全权控制 Docker 启停沙盒，无论它跑在宿主还是容器里都需要这个权限。supervisor 容器内 user 是 root 还是 non-root 在 socket 已挂的语境下不构成实质安全差异；镜像仍保留 non-root `Expert Work` 账户作镜像卫生基线，compose 服务用 `user: root` 跑以规避宿主 `docker` 组 GID 不可移植问题。M1 转 K8s + RuntimeClass=gvisor 后此 ADR 失效（M3 roadmap 已列 sandbox 转 K8s Pod）。

### Mini-ADR I-3：全栈 compose 拓扑 —— `full` profile + 一次性 `migrate` 服务

- **背景**：`infra/docker-compose.yml` 现有数据层（无 profile，恒起）+ nginx（`proxy` profile）/ keycloak（`auth`）/ credential-proxy（`sandbox`）。I.1 要把 control-plane + sandbox-supervisor 接进来，并让一条命令起 M0 完整栈。
- **决策**：
  1. **`migrate` 一次性服务**：用 control-plane 镜像跑 `alembic upgrade head` 后退出（迁移在 `packages/expert-work-persistence/migrations/`，已在该镜像依赖闭包内）。control-plane / sandbox-supervisor / credential-proxy 经 `depends_on: { migrate: { condition: service_completed_successfully } }` 等它完成。
  2. **`full` profile**：`migrate` / `control-plane` / `sandbox-supervisor` / `credential-proxy` 都标 `profiles: ["full"]`；credential-proxy 同时保留既有 `["sandbox"]`（数组并列）。`docker compose --profile full up` = M0 完整应用栈。
  3. control-plane 连 pgbouncer（6432）、redis（quota）；env `EXPERT_WORK_SANDBOX_SUPERVISOR_URL` 指向 `http://sandbox-supervisor:8000`。sandbox-supervisor 直连 postgres（5432）+ 挂 docker.sock。
- **理由**：(1) 一次性 migrate + `service_completed_successfully` 是 compose 跑 schema 迁移的标准姿势 —— 避免每个服务各自 `alembic upgrade` 竞争、避免"服务起来表没建"。复用 control-plane 镜像跑迁移，不额外建 migrate 镜像。(2) `full` profile 与既有 `proxy` / `auth` / `sandbox` 一致 —— 数据层集成测试的默认 `up` 不受影响（不会因 I.1 多绑端口 / 多拉镜像而变慢）。
- **代价**：profile 组合略多（`proxy` / `auth` / `sandbox` / `full`）；在 compose 文件注释里给出 profile 速查表。

---

## 4. Verification

| 验证 | 手段 |
|------|------|
| 全栈拉起 | `docker compose --profile full up` → `docker compose ps` 所有服务 healthy（migrate 显示 exited 0）|
| 镜像合规 | control-plane / sandbox-supervisor 镜像：多阶段、non-root entrypoint、`docker history` 不含构建期工具链 |
| #60 egress e2e | `Test (integration)` job 跑 `test_fullstack_egress_e2e.py`（I.1b）|
| 单元回归 | `uv run pytest -m "not integration" -q` 全绿（I.1 不碰 Python 源码，预期零回归）|
| lint / 类型 | `ruff check` + `ruff format --check` + `pre-commit run --files <改动>` + CI-scope `mypy` |
| CI | 8/8（`Test (integration)` 首次跑全栈 e2e）|

### 测试矩阵（接续 Stream F，F 收于 #59）

| # | 用例 | 子项 | 层级 | 说明 |
|---|------|------|------|------|
| 60 | 全栈 egress e2e | I.1b | integration | 起 egress 子栈 → 直接经 `ExecPythonTool` 跑沙盒 → 沙盒内 stdlib 代码经 credential-proxy 出网到 `mock-upstream` → 断言：(a) `mock-upstream` 收到的请求带 proxy 注入的 `Authorization: Bearer <secret>`；(b) 沙盒拿到响应；(c) `credential_proxy_audit` 落 ref + host + status、**无明文 secret**。原属 Stream F.11，移入 I.1 |

### 4.1 #60 全栈 egress e2e 设计（I.1b 细化）

I.1a 落地后对 #60 的链路做了实测细化，三处偏离 § 4 原草图：

- **入口不走 control-plane run API，直接构造 `ExecPythonTool`**。control-plane 的 `POST /v1/sessions/{id}/runs` 要先经 LLM 推理才决定调 `exec_python` —— 需真 LLM 凭据（STREAM-E Verification 已把 holistic agent run 归 M0→M1 Gate dogfood）。#60 验的是 **egress 链路**（sandbox → proxy → upstream），不是 LLM→tool 决策。故测试直接 `ExecPythonTool(client=HTTPSupervisorClient(base_url=...))` + `tool.call({code}, ctx=ToolContext(...))`，绕过 LLM / orchestrator graph —— 这正是 `#60` 名字里 "exec_python" 的入口。
- **不起 `full` profile，只起 egress 子栈**。#60 只需 `postgres` + `migrate` + `credential-proxy` + `sandbox-supervisor` + `mock-upstream` —— 不需要 control-plane / redis。起 `full` 会拉 redis，而 redis 是 profile-less（恒起）服务，宿主已占 6379 时本地必冲突。测试 fixture 改为 `docker compose up -d --wait <显式服务名>` —— 显式点名可启动 profile-gated 服务（compose 行为），且**不**触发 profile-less 的 redis/minio/pgbouncer。
- **沙盒代码用 stdlib `urllib.request`，非 `httpx`**。沙盒镜像无 pip、纯 stdlib（Mini-ADR F-1）。

链路与新增物：

1. **`mock-upstream` 服务**（新）：极简 HTTP echo（`infra/mock-upstream/echo_server.py`，stdlib `http.server`），把收到的 method / path / headers / body 回 JSON。挂 `default` 网，credential-proxy 出网侧经服务名 `mock-upstream` 可达。`profiles: ["e2e"]` —— 仅 e2e 显式点名时启动，不污染 `full` dev 栈。
2. **credential-proxy 注入用 secret**：credential-proxy `local_dev` SecretStore 当前为空。挂一个 dev fixture secret 文件（`infra/credential-proxy/secrets.env`，占位值，与 compose 里 `expert_work_dev` 同性质），设 `EXPERT_WORK_CRED_PROXY_SECRET_STORE_ENV_FILE`。
3. **sandbox-supervisor 暴露宿主端口** `8001:8000`：测试进程在宿主，需经 HTTP 驱动 supervisor。
4. **`secret_allowlist` 种行**：测试经宿主 `localhost:5432` 直连 compose Postgres，插 `(tenant, agent_name, agent_version, secret_ref)` 行；测试结束清理。
5. **沙盒可达 proxy**：composed supervisor 经 DooD `docker run` 沙盒、join `expert-work-sandbox-egress`；沙盒代码 `POST http://credential-proxy:8080/forward`，带 `X-Expert-Work-Tenant/Agent/Agent-Version/Secret-Ref/Upstream` 头；proxy 校 allowlist → 解析 secret → 注入 `Authorization: Bearer <secret>` → 转发 `X-Expert-Work-Upstream`（= `http://mock-upstream:<port>/...`）。
6. **`expert-work-sandbox:dev` 镜像**：composed supervisor 默认用该 tag 起沙盒；fixture 先 `docker build` 它（同 F.8 harness build `expert-work-sandbox:itest` 的做法）。

测试位置 `services/control-plane/tests/test_fullstack_egress_e2e.py`（control-plane 同时依赖 `orchestrator` + `expert-work-persistence`，导入闭包齐全），`@pytest.mark.integration`，走非 gating `Test (integration)` job；Docker / compose 不可用时整体 skip（同 F.8）。

---

## 5. PR 顺序

| PR | 内容 | 验证 |
|----|------|------|
| **I.1a** | 本设计文档；control-plane / sandbox-supervisor Dockerfile；compose 加 `migrate` + 两服务 + `full` profile；ITERATION-PLAN I.1 修订；`infra/README` 文档化 sandbox 镜像预构建 | `docker compose --profile full up` 全栈 healthy |
| **I.1b** | `mock-upstream` compose 服务；`test_fullstack_egress_e2e.py`（#60）；测试矩阵勾 #60 | #60 在 `Test (integration)` job 绿 |

---

## 6. I.2 — 发布策略（蓝绿 + 金丝雀）

> 落实 P0 #32（07-INFRASTRUCTURE-GAPS §10「服务发布策略 —— 蓝绿/金丝雀」）。

### 6.1 前提：control-plane 已无状态

ADR B-6（SQL store 切换）后 control-plane 的全部状态落在 Postgres / Redis —— **同一套数据库上并行跑两个 control-plane 实例是安全的**。这正是蓝绿的硬前提；I.2 直接建立在它之上（自下而上：I.1 容器化 + B-6 无状态化 → I.2 发布策略）。

### 6.2 In-scope / Out-of-scope

| | 内容 |
|---|---|
| **In-scope（I.2）** | control-plane 经 nginx upstream 切换的蓝绿发布；分阶段加权金丝雀；`tools/deploy/deploy.py` 部署脚本（healthcheck 闸 + 金丝雀步进 + 全量切换 + 旧色 drain）|
| **Out-of-scope** | sandbox-supervisor / credential-proxy 的蓝绿（内部服务，用 healthcheck-gated recreate —— Mini-ADR I-4）；K8s RuntimeClass / Helm 渐进发布（M1+）；按租户灰度（P1，07-GAPS §7「租户级灰度」）；自动化金丝雀分析 / 自动回滚判定（M1，金丝雀步进 M0 由人看 Stream G 的 SLO 大盘后手动推进）|

### 6.3 机制

**拓扑**：nginx（已存在，`proxy` profile，Stream C.2）front control-plane。I.2 把蓝绿做在 nginx 的 `upstream` 上。

```
                    ┌─ control-plane-blue:8000   (当前 live)
nginx upstream ──────┤
control_plane_upstream└─ control-plane-green:8000  (新版本，部署中)

upstream 块抽到 include 文件 infra/nginx/conf.d/control-plane-upstream.conf：
  全量:   server control-plane-blue:8000;
  金丝雀: server control-plane-blue:8000 weight=9;
          server control-plane-green:8000 weight=1;
deploy.py 重写该文件后 `nginx -s reload`（reload 不断连，平滑换 upstream）。
```

- **compose**：新增 `control-plane-blue` / `control-plane-green` 两个服务 —— YAML anchor 共用配置，仅 `container_name` / host 端口（blue 8000、green 8001）不同；`build:` 只挂 blue 上（green / migrate 复用同 tag，避免重复构建竞争，同 Mini-ADR I-3 对 migrate 的处理）。两色都 `profiles: ["full"]`，随全栈常驻；deploy 时 `--force-recreate` **原地重建 idle 色**（带新 tag），live 色不动 —— 两色常驻消除了「冷启动该拉哪个色」的歧义。原单个 `control-plane` 服务由这两者取代。
- **deploy.py 流程**（输入：新镜像 tag）：
  1. 读 nginx upstream include 文件判定 live 色 → idle 色（另一个）。
  2. `docker compose up -d --no-deps --force-recreate control-plane-<idle>`（idle 色已在跑，带新 tag 原地重建它）。
  3. 轮询 idle 色 `/healthz/ready` —— readiness 含 DB / Redis 依赖检查（A.11）；超时未 healthy → 中止，不切流量，idle 色留存供排查。
  4. **金丝雀**（可选 `--canary 10,50`）：依次把 nginx 权重设为 `idle=10 / live=90`、`idle=50 / live=50`，每步之间 `--canary-pause` 秒（人看 Stream G SLO 大盘判断是否继续；脚本只负责步进，不做自动分析）。
  5. 全量切换：upstream 写成只剩 idle 色 → `nginx -s reload`。
  6. 旧 live 色 drain：发 `SIGTERM`，优雅停机（A.12）排空在途请求 / run；超 `--drain-timeout` 强停。**旧色容器不删**（停止保留），供 I.3 秒级回滚。
- **schema 变更的发布**：若新版本带迁移 → 先跑 `migrate`（只做 expand，见 § 7.3）→ 再蓝绿换 control-plane。迁移与代码切换解耦，保证两个色在切换窗口内都对 schema 兼容。
- **内部服务**（sandbox-supervisor / credential-proxy）：非用户面，用 healthcheck-gated recreate —— `docker compose up -d <svc>` 重建，healthcheck 不过则 compose 报错、旧容器视情况留存。M0 不为其做蓝绿（Mini-ADR I-4）。

### 6.4 多实例正确性

蓝绿切换窗口内两个色并存（数十秒～数分钟）。M0 默认 `single_instance=true` 的进程内限流器（ADR B-1）在此窗口会**每色各算一份** → 限流偏松。**做蓝绿的环境（staging / prod）必须设 `EXPERT_WORK_SINGLE_INSTANCE=false` + `EXPERT_WORK_QUOTA_REDIS_URL`** —— 网关 / 租户限流器切到 Redis 后端（`create_app` 已支持该分支），多进程下计数正确。I.4 的三环境文档把这条列为 staging / prod 必设项。

### 6.5 Mini-ADR I-4：蓝绿范围 = 仅 control-plane，经 nginx upstream 切换

- **背景**：M0 在线栈 = control-plane + sandbox-supervisor + credential-proxy 三个 Expert Work 服务。蓝绿要不要覆盖全部三个？
- **选项**：(a) 仅 control-plane 蓝绿，内部服务 recreate；(b) 三个服务都蓝绿；(c) 引 K8s + RuntimeClass 做滚动发布。
- **决策**：(a)。control-plane 是**唯一用户面**服务、ADR B-6 后**无状态**、有优雅停机（A.12）+ Postgres checkpointer（E.1，在途 run 的 graph state 持久）—— 蓝绿天然适配且零停机价值最高。sandbox-supervisor / credential-proxy 是内部服务，短暂重建期间至多让"新触发的 `exec_python`"重试一次，蓝绿对它们收益低、复杂度高（sandbox-supervisor 还涉及 DooD socket、运行中沙盒容器归属），用 healthcheck-gated recreate 足够。
- **理由**：(b) 双倍内部服务实例、且 sandbox-supervisor 双色同时操作宿主 Docker 易混淆运行中沙盒归属；(c) K8s 是 M1/M3 roadmap（Mini-ADR I-2 已述 sandbox 转 K8s 推 M3），M0 单台 ECS + docker-compose 不引入。
- **代价**：内部服务发布有秒级重建空窗 —— 已在 § 2.5 失败模式登记（`exec_python` 调用失败返回明确工具错误，不影响 control-plane 存活）。M1 转 K8s 后此 ADR 由 RollingUpdate 取代。

---

## 7. I.3 — 回滚 + DB 兼容

> 落实 P0 #33（07-INFRASTRUCTURE-GAPS §10「服务回滚 —— 一键回滚 + 数据库迁移兼容」）。

### 7.1 In-scope / Out-of-scope

| | 内容 |
|---|---|
| **In-scope（I.3）** | `tools/deploy/rollback.py` 一键回滚；expand-contract 迁移纪律写成文档 + 发布检查清单强制项 |
| **Out-of-scope** | `alembic downgrade` 自动降级（不采用 —— 见 7.3）；迁移 linter / CI 自动拦 contract 迁移（M1-B「DB zero-downtime migration 规范」）；自动回滚判定（M1）|

### 7.2 一键回滚

- **快路径**（同版本回退一步）：蓝绿切换后旧色容器**保留不删**（§ 6.3 步骤 6）。回滚 = `rollback.py` 把 nginx upstream 写回旧色 → `nginx -s reload`。秒级、零重建、零拉镜像 —— 旧色进程一直在跑。
- **兜底路径**（旧色已被下一次发布顶掉 / 跨多版本回退）：`rollback.py --to-tag <旧 tag>` 以指定旧镜像 tag 重跑一次 § 6.3 的蓝绿部署流程。
- 一条命令：`python tools/deploy/rollback.py`（默认快路径）/ `--to-tag X`（兜底）。

### 7.3 DB 兼容（向前/向后）= expand-contract 纪律

**回滚安全的硬约束**：回滚到旧 control-plane 镜像后，旧代码必须能跑在**当前** schema 上。M0 用 **expand-contract** 迁移纪律保证，而非 `alembic downgrade`：

- **expand（允许随发布走）**：加列（nullable 或带 default）、加表、加索引（`CREATE INDEX CONCURRENTLY`）、加宽类型。对旧代码恒兼容 —— 旧代码看不见新列也能跑。
- **contract（单独发布、滞后）**：删列、改名、加 `NOT NULL`、窄化类型。只能在「依赖旧 shape 的代码已全部下线」之后的**单独一个发布**里做。
- **推论**：任一发布 v(n) 的 schema 对 v(n-1) 代码恒兼容 → 蓝绿回滚到 v(n-1) 镜像永远安全。
- **不用 `alembic downgrade`**：生产降级迁移可能丢数据（如降级要删掉新表 / 新列里已写入的数据），且降级脚本极少被测。兼容性靠"只 expand"的纪律实现，迁移**只向前**。
- **落地形态（M0）**：在 `docs/runbooks/deployment.md` 写明 expand-contract 规则 + 在发布检查清单（§ 8.3）列为强制勾选项「本次迁移是否纯 expand？含 contract 则确认旧代码已下线 ≥1 个发布」。完整的迁移 linter / CI 自动拦截属 M1-B「DB zero-downtime migration 规范」，I.3 不做自动化、只立规则与检查点。

### 7.4 Mini-ADR I-5：回滚 = nginx 切色（快）+ 镜像 tag 重部署（兜底）；DB 兼容靠 expand-contract，不靠 downgrade

- **背景**：P0 #33 要「一键回滚 + 数据库迁移兼容（向前/向后）」。
- **决策**：回滚走两条路径（快路径 nginx 切回保留的旧色；兜底 `--to-tag` 重部署）；DB 双向兼容靠 expand-contract 迁移纪律，迁移只向前、永不 `downgrade`。
- **理由**：(1) 旧色容器在蓝绿后本就保留（下次发布前一直在），切回它是真正的秒级回滚，比"重新拉旧镜像部署"快一个量级。(2) `alembic downgrade` 在生产不可靠（丢数据风险 + 降级路径未测）；expand-contract 让"schema 永远兼容前一版代码"成为不变量，回滚因此天然安全 —— 不需要"回滚 schema"这个动作。
- **代价**：开发者要遵守 expand-contract（含 contract 的变更要拆成跨发布的两步）；M0 靠发布清单人工把关，M1-B 上自动 linter。旧色容器常驻占一份内存 —— 单台 ECS 可接受（control-plane 无状态、轻量），下次发布时被回收。

---

## 8. I.4 — 三环境部署文档

> 落实 07-INFRASTRUCTURE-GAPS §10「环境隔离」(P0) + 为 dogfood / M0→M1 Gate 提供可操作的部署 runbook。

### 8.1 In-scope / Out-of-scope

| | 内容 |
|---|---|
| **In-scope（I.4）** | `docs/runbooks/deployment.md` —— 三环境矩阵、部署步骤、回滚步骤、发布检查清单；补实 `environments/{dev,staging,prod}.yaml`（dev 完整可用；staging / prod 结构齐全，云端点待 provision 时填）|
| **Out-of-scope** | Terraform / IaC（M1-B 已列）；CD 自动触发（GitHub Actions 自动部署 —— M1）；阿里云资源 provision 本身（基础设施工单，非本 Stream）|

### 8.2 三环境矩阵

| 维度 | dev | staging | prod |
|------|-----|---------|------|
| 载体 | 本机 docker-compose（OrbStack/Lima）| 单台阿里云 ECS + docker-compose | 阿里云 ECS + docker-compose（独立实例）|
| Postgres | compose `postgres` 容器 | 阿里云 RDS（staging 库）| 阿里云 RDS（prod 库，独立实例）|
| 对象存储 | compose MinIO | 阿里云 OSS（staging bucket）| 阿里云 OSS（prod bucket）|
| Secret | `local_dev`（`infra/credential-proxy/secrets.env` 占位）| 阿里云 KMS Secrets Manager（ADR-0007）| 阿里云 KMS Secrets Manager |
| 镜像来源 | 本地 `docker build` | 阿里云 ACR | 阿里云 ACR |
| TLS 证书 | 自签（`tools/dev-certs/`）| 内部 CA / 真证书 | 真证书 |
| `store_backend` | `sql`（compose `migrate` 起 schema）| `sql` | `sql` |
| `single_instance` | `true`（不蓝绿）| `false` + Redis（蓝绿需要，§ 6.4）| `false` + Redis |
| `EXPERT_WORK_APIKEY_RATE_LIMIT_HMAC_SALT` | 未设置（单副本用进程随机盐即可）| **必配**，所有副本同值（`openssl rand -hex 32` 生成）| **必配**，所有副本同值 |
| 发布方式 | `compose up`（无蓝绿）| `deploy.py` 蓝绿 | `deploy.py` 蓝绿 + 金丝雀 |

**环境隔离（P0）**：三环境的 DB / 密钥 / bucket 完全分离，命名一律带环境后缀（`expert_work_dev` / `_staging` / `_prod`）防误连；`environments/<env>.yaml` + `EXPERT_WORK_*` env var + KMS 三处配置源，prod 凭据只存 KMS、不落盘不进仓库。

### 8.3 deployment.md 内容骨架

1. 三环境矩阵（§ 8.2）。
2. 配置来源与优先级：`environments/<env>.yaml`（结构化非密配置）→ `EXPERT_WORK_*` env var → KMS（密钥）。
3. 首次部署步骤（per env）：备好 `environments/<env>.yaml` + env → 拉镜像（ACR / 本地 build）→ `docker compose --profile full up`（dev）/ 起数据层 + `migrate` + 蓝绿（staging/prod）。
4. 滚动发布步骤：`migrate`（expand，§ 7.3）→ `deploy.py --tag <new>`（→ § 6.3）。
5. 回滚步骤：`rollback.py`（→ § 7.2）。
6. **发布检查清单**：迁移是否纯 expand（§ 7.3）/ 新镜像 CI 全绿 / `environments/<env>.yaml` 已核对 / 金丝雀阶段已看 SLO 大盘（Stream G）/ 旧色保留待回滚。
7. 环境差异与坑：dev 的 redis 占宿主 6379（已知，见 § 4.1）、staging/prod 必设 `single_instance=false`、staging/prod 必设 `EXPERT_WORK_APIKEY_RATE_LIMIT_HMAC_SALT`（所有副本同值，`openssl rand -hex 32` 生成；未配则每副本各自随机盐，同一 api key 在 N 副本散 N 个桶，apikey 维度限流形同放宽 N 倍）、prod 密钥只走 KMS。

### 8.4 说明

I.4 以文档为主，不引 Mini-ADR。`environments/staging.yaml` / `prod.yaml` 现为占位（云端点 `TBD-*`）—— I.4 补全其**结构**与非密字段，真实端点待阿里云 ECS / RDS / OSS / ACR / KMS provision 后由基础设施工单回填；deployment.md 的步骤与清单不依赖端点取值，先行可用。

---

## 9. PR 顺序 & Verification（I.2 – I.4）

| PR | 内容 | 验证 |
|----|------|------|
| **I.2** | § 6；compose 拆 `control-plane-blue/green`；nginx upstream 抽 include 文件；`tools/deploy/deploy.py`（蓝绿 + 金丝雀步进）| 本机 `--profile full up` → `deploy.py` 部署一个新 tag → 测试矩阵 #69：起全栈、deploy.py 蓝绿换色、经 nginx `/healthz/ready` 验新色接流、旧色 drain（`@pytest.mark.integration`，Docker 不可用则 skip）|
| **I.3** | § 7；`tools/deploy/rollback.py`；`deployment.md` 的 expand-contract 章节 + 发布清单 | 测试矩阵 #70：deploy 换色后 `rollback.py` 切回旧色、经 nginx 验流量回到旧色（`@pytest.mark.integration`）；`deploy.py`/`rollback.py` 的纯逻辑（色判定、upstream 文件渲染、权重计算）走单测，不需 Docker |
| **I.4** | § 8；`docs/runbooks/deployment.md`；补全 `environments/{staging,prod}.yaml` 结构 | 文档评审；`tools/tls/check_tls_config.py` 既有的 `environments/*.yaml` 解析不被破坏 |

> 三个 PR 各自独立可合：I.2 落部署脚本，I.3 在其上加回滚，I.4 是文档 + 配置。I.3 的快路径回滚依赖 I.2 的"旧色保留"，故 I.3 在 I.2 之后。

### 9.1 测试矩阵（接续，I.1 收于 #60）

| # | 用例 | 子项 | 层级 | 说明 |
|---|------|------|------|------|
| 69 | 蓝绿部署 smoke | I.2 | integration | 起 `full` 栈 → `deploy.py` 用新 tag 蓝绿换色 → 断言 nginx upstream 已切、新色经 `/healthz/ready` 接流、旧色收到 SIGTERM 后优雅退出 |
| 70 | 一键回滚 smoke | I.3 | integration | 在 #69 之后 → `rollback.py` 快路径 → 断言 nginx upstream 切回旧色、流量回旧色；外加 deploy/rollback 纯函数单测（色判定 / upstream 渲染 / 权重）|
