# 03 仓库目录结构

> **2026-05-11 更新**：Python 命名空间从 `expert_work.X` 统一为 `expert_work.X`，与项目名 Expert Work + 现有 `expert_work` 包对齐；增加 `environments/` 顶层目录承载三环境配置；增加"创建策略"一节说明 just-in-time 建包原则。

## Monorepo 布局

```
expert-work/
├── README.md
├── pyproject.toml                  # workspace root（uv）
├── docker-compose.yml              # → deploy/docker-compose/dev.yml 的软链
├── .pre-commit-config.yaml
├── .github/workflows/              # CI: lint / test / image build / vendor sync check
│
├── environments/                   # 三环境配置（dev / staging / prod）
│   ├── dev.yaml
│   ├── staging.yaml
│   └── prod.yaml
│
├── deploy/
│   ├── docker-compose/
│   │   ├── dev.yml
│   │   ├── prod.yml
│   │   └── observability.yml
│   ├── k8s/                        # M3 阶段填充
│   │   ├── helm/expert-work/
│   │   └── operator/
│   └── images/
│       ├── sandbox-base.Dockerfile
│       ├── sandbox-python.Dockerfile
│       └── sandbox-node.Dockerfile
│
├── packages/                       # 可复用 Python 包（workspace members）
│   ├── expert-work-sdk/             # Agent 作者用：@tool、AgentContext、AgentState
│   │   ├── pyproject.toml
│   │   └── src/expert_work/sdk/
│   │       ├── __init__.py
│   │       ├── tool.py             # @tool 装饰器
│   │       ├── context.py          # AgentContext, BuildContext, HookContext
│   │       ├── state.py            # AgentState (TypedDict)
│   │       └── middleware/         # AgentMiddleware 基类（vendor + 自建）
│   │
│   ├── expert-work-protocol/        # 跨服务 schema：proto / Pydantic
│   │   ├── pyproject.toml
│   │   └── src/expert_work/protocol/
│   │       ├── __init__.py
│   │       ├── agent_spec.py       # AgentSpec、AgentManifest
│   │       ├── session.py          # SessionEvent、RunEvent
│   │       ├── tool.py             # ToolCall、ToolResult
│   │       └── sandbox.py          # SandboxHandle、ExecRequest
│   │
│   ├── expert-work-runtime/         # 基础设施层（vendor 自 deer-flow harness）
│   │   ├── pyproject.toml
│   │   └── src/expert_work/runtime/
│   │       ├── event_log/          # 🔴 P0 vendor: deer-flow runtime/events/store/
│   │       │   ├── base.py
│   │       │   ├── db.py
│   │       │   └── memory.py
│   │       ├── checkpointer/       # 🔴 P0 vendor: runtime/checkpointer/
│   │       ├── store/              # 🔴 P0 vendor: runtime/store/
│   │       ├── stream/             # 🔴 P0 vendor: runtime/stream_bridge/
│   │       ├── runs/               # 🔴 P0 vendor: runtime/runs/
│   │       └── context.py          # 🔴 P0 vendor: runtime/user_context.py（改 tenant）
│   │
│   ├── expert-work-persistence/     # ORM + 多租户权限
│   │   ├── pyproject.toml
│   │   └── src/expert_work/persistence/
│   │       ├── base.py             # 🔴 P0 vendor: persistence/base.py
│   │       ├── engine.py           # 🔴 P0 vendor: persistence/engine.py
│   │       ├── models/
│   │       │   ├── thread_meta.py  # 🔴 P0 vendor + tenant 字段
│   │       │   ├── run.py          # 🔴 P0 vendor
│   │       │   ├── run_event.py    # 🔴 P0 vendor
│   │       │   └── user.py
│   │       ├── thread_meta/        # Repository pattern
│   │       └── migrations/         # Alembic
│   │
│   └── expert-work-common/          # logger、telemetry、错误类、版本号
│       ├── pyproject.toml
│       └── src/expert_work/common/
│           ├── __init__.py         # 暴露 __version__
│           ├── logging.py
│           ├── telemetry.py
│           ├── errors.py
│           └── config_loader.py
│
├── services/                       # 可独立部署的服务
│   ├── control-plane/
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   └── src/control_plane/
│   │       ├── api/                # FastAPI routers
│   │       │   ├── agents.py
│   │       │   ├── sessions.py
│   │       │   ├── runs.py         # SSE
│   │       │   └── admin.py
│   │       ├── domain/             # AgentSpec、Session 应用对象
│   │       ├── manifest/           # YAML loader + Pydantic 校验 + 静态分析
│   │       │   ├── loader.py       # 主入口
│   │       │   ├── jinja.py        # Jinja2 渲染
│   │       │   ├── analyzer.py     # 静态分析 lint
│   │       │   └── signer.py       # cosign 集成
│   │       ├── auth/               # JWT、RBAC、租户解析
│   │       ├── registry/           # AgentSpec 版本注册表
│   │       └── main.py
│   │   └── tests/
│   │
│   ├── orchestrator/
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   └── src/orchestrator/
│   │       ├── runtime/            # Harness loop
│   │       ├── graph_builder/      # AgentSpec → LangGraph (隔离 LangGraph 接触)
│   │       │   ├── builder.py
│   │       │   ├── tool_dispatcher.py
│   │       │   └── workflow_react.py / workflow_plan_execute.py
│   │       ├── tools/              # 内置 tools（web_search、view_image 等）
│   │       │   └── builtins/
│   │       ├── llm/                # provider router + fallback
│   │       ├── memory/             # Memory store wrapper（pgvector）
│   │       ├── middleware/         # 🟠 P1 vendor + 自建
│   │       │   ├── error_handling.py    # vendor: tool_error_handling_middleware.py
│   │       │   ├── memory.py            # vendor: memory_middleware.py
│   │       │   ├── token_usage.py       # vendor: token_usage_middleware.py
│   │       │   ├── loop_detection.py    # vendor
│   │       │   ├── rate_limit.py        # 自建
│   │       │   └── credential.py        # 自建（凭证解析）
│   │       └── checkpointer/       # 用 expert-work-runtime/checkpointer
│   │   └── tests/
│   │
│   ├── sandbox-supervisor/
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   └── src/sandbox_supervisor/
│   │       ├── pool/               # warm pool, lifecycle, quota
│   │       ├── runtime/            # docker SDK + runsc 适配
│   │       ├── rpc/                # gRPC server
│   │       ├── builder/            # image_build：自动构建定制镜像
│   │       └── agent/              # 跑在 sandbox 内的 supervisor agent
│   │   └── images/
│   │       └── sandbox_inner_agent.py
│   │
│   ├── credential-proxy/
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   ├── envoy.yaml              # M1+ Envoy 配置
│   │   ├── filters/                # Lua 脚本
│   │   └── src/credential_proxy/   # M0 自研 aiohttp 版
│   │       ├── proxy.py
│   │       ├── vault_client.py
│   │       └── policy.py           # secret 引用解析 + 审计
│   │
│   ├── mcp-gateway/
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   └── src/mcp_gateway/
│   │       ├── client.py           # 🟠 P1 vendor: mcp/client.py
│   │       ├── pool.py             # 多 server 连接池
│   │       └── auth.py             # OAuth/API key 注入
│   │
│   └── admin-ui/                   # React 19 + Vite + Antd（复用现有）
│       ├── package.json
│       ├── Dockerfile
│       └── src/
│
├── examples/
│   └── agents/
│       ├── code-reviewer-agent/    # 纯 YAML 示例（业务无关）
│       │   └── manifest.yaml
│       ├── ticket-classifier/      # Python 插槽示例（客服工单分类）
│       │   ├── manifest.yaml
│       │   ├── tools.py
│       │   ├── graph.py
│       │   └── hooks.py
│       └── security-auditor/       # subagent 引用示例
│           └── manifest.yaml
├── templates/                      # 领域模板包（可选；业务按需引用）
│   ├── dev-tools/                  # 代码 review / 测试生成 / 文档总结
│   ├── customer-service/           # 工单分类 / 客户回复
│   ├── medical/                    # 启用 hipaa pack + 医疗 guardrails
│   └── hr/                         # HR 工作流
│
├── docs/
│   ├── architecture/               # 本目录（架构方案）
│   ├── research/                   # 调研附录
│   ├── manifest-spec.md            # YAML schema 完整规范
│   ├── sandbox-security.md         # 安全模型
│   ├── runbook/                    # 运维 SOP
│   └── adr/                        # Architecture Decision Records
│
└── tools/
    ├── expert-work-cli/             # CLI：lint manifest、本地 run、debug
    │   ├── pyproject.toml
    │   └── src/expert_work_cli/
    │       ├── lint.py
    │       ├── run.py              # 本地 docker run，单 agent 调试
    │       └── debug.py
    └── perf-bench/                 # 性能基准脚本
```

---

## 关键文件路径（M0 第一批要创建）

| 路径 | 作用 |
|------|------|
| `services/control-plane/src/control_plane/manifest/loader.py` | YAML 加载/Pydantic 校验入口(无模板阶段) |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py` | AgentSpec → LangGraph StateGraph 编译核心 |
| `services/sandbox-supervisor/src/sandbox_supervisor/pool/supervisor.py` | Sandbox 池化、Docker+gVisor 调度 |
| `services/credential-proxy/src/credential_proxy/proxy.py` | 凭证注入代理（M0 自研版）|
| `packages/expert-work-sdk/src/expert_work/sdk/__init__.py` | `@tool`、`AgentContext`、`AgentState` 公共 SDK |
| `packages/expert-work-runtime/src/expert_work/runtime/event_log/db.py` | event_log 实现（vendor）|
| `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py` | AgentSpec Pydantic schema |
| `deploy/docker-compose/dev.yml` | 单机起步全栈编排 |
| `examples/agents/code-reviewer-agent/manifest.yaml` | 首个 manifest（业务无关黄金参考样本）|

---

## Vendor 文件标记规范

每个从 deer-flow vendor 来的文件，头部必须有标准注释：

```python
# ============================================================
# Adapted from bytedance/deer-flow @ <commit_sha>
# Source: backend/packages/harness/deerflow/runtime/events/store/db.py
# License: MIT (see vendor/deer-flow/LICENSE)
# Modifications:
#   - Replaced user_id contextvar with tenant_id
#   - Extended event_type enum
#   - Added pipeline_id for multi-stage tracking
# Last sync: 2026-05-09
# ============================================================
```

定期 vendor sync（季度）：检查 deer-flow 上游有无重要 bug fix，按需手工 patch。

---

## 创建策略 — Just-in-Time per Stream

> 2026-05-11 补充：本仓库是单人项目，**不一次性把所有 packages/services 骨架建出来**（避免空文件污染 + 阅读负担）。每个 package / service 在**首次实施它的 Stream 启动时按需创建**。

### Phase 0.2 只建以下

| 路径 | 目的 |
|------|------|
| `pyproject.toml`（root） | uv workspace 配置；初始 members 仅 `expert-work-common` |
| `packages/expert-work-common/` | 引导真包，承载 `__version__` + 后续 logger / telemetry / errors；替代原 `src/expert_work/__init__.py` 占位 |
| `tests/conftest.py` | 测试 fixture 入口（空骨架） |
| `environments/dev.yaml` | 三环境配置占位（staging/prod 等基础设施到位后补） |
| `.pre-commit-config.yaml`、`ruff.toml`/在 pyproject.toml、`mypy.ini`/在 pyproject.toml | 工具链 |

### 其他 packages / services 创建时机

| 包 / 服务 | 创建于 | 触发条件 |
|----------|--------|---------|
| `packages/expert-work-persistence` | Stream A 开始 | A.1 Postgres schema 实施 |
| `packages/expert-work-runtime` | Stream A 开始 | A.2 Vendor DeerFlow P0 基础设施实施 |
| `packages/expert-work-protocol` | Stream B 开始 | B.4 Manifest schema 编写 |
| `packages/expert-work-sdk` | Stream E 开始 | E.6 ReAct mode 需要暴露 @tool 装饰器 |
| `services/control-plane` | Stream B 开始 | B.1 FastAPI 骨架 |
| `services/orchestrator` | Stream E 开始 | E.1 LangGraph PostgresSaver 接入 |
| `services/sandbox-supervisor` | Stream F 开始 | F.1 Sandbox Supervisor 服务 |
| `services/credential-proxy` | Stream F 开始 | F.5 Credential Proxy aiohttp 自研版 |
| `services/mcp-gateway` | Stream E 开始 | E.9 工具：MCP 接入 |
| `services/admin-ui` | Stream H 开始 | H.1 React 骨架 |
| `tools/expert-work-cli` | M1-I 开始 | M1 CLI + Admin UI 升级 |
| `templates/` | 按业务模板需要 | M1-F 多租户 + 业务模板使用时 |

### 创建新包 / 服务时的标准动作

1. 在 root `pyproject.toml` 的 workspace members 加路径
2. 在新包 / 服务下建 `pyproject.toml`（按现有 `expert-work-common` 模板）
3. 建 `src/<namespace>/<subpkg>/__init__.py` 骨架
4. 在 `.github/workflows/codeql.yml`（如有 path filter）+ `dependabot.yml` 加扫描路径（如需要）
5. 在本 Stream 的设计文档（`docs/architecture/subsystems/xx-*.md` 或对应已有文档）落地组件设计
6. 跑一次 `uv sync` 确认 workspace 解析通过

定期 vendor sync（季度）：检查 deer-flow 上游有无重要 bug fix，按需手工 patch。
