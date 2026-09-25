# 02 Agent 配置机制（最关键设计点）

## 三层抽象

```
AgentManifest (YAML)        — 用户写的声明（意图）
   ↓ Pydantic + JSON Schema 校验
AgentSpec (强类型对象)        — 运行时契约
   ↓ GraphBuilder
CompiledAgent (StateGraph)   — LangGraph 编译产物
   ↓ Registry (按 version pin)
Hot-swap 热更新              — 旧 session 用旧 graph，新 session 用新 graph
```

### 加载流水线

```
[Watch FS / Git / DB]  →  [Lexer: YAML+Jinja2 渲染]
                          →  [Pydantic 验证: AgentSpec]
                          →  [Static Analyzer: 权限/沙盒边界检查]
                          →  [GraphBuilder: 编译 StateGraph]
                          →  [Sandbox Image Builder: 按需构建]
                          →  [Registry: agent_name@version 注册]
                          →  [Hot Swap: 新流量走新版本，旧 session 走旧版]
```

---

## 80% 场景 — 纯声明式 YAML（运维可写，无需代码）

> **示例选择说明**：以下例子刻意选**业务无关**的内部研发场景（代码 review），让 manifest 模型对所有租户都直观。医疗、客服、HR 等具体业务通过下文 `tenant_config.compliance_pack` + `extends` 模板包注入差异化，而**不是把领域知识写死在 manifest 里**。

```yaml
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer-agent
  version: "1.4.2"            # 语义化版本，热更新按 version pin
  tenant: platform-eng        # 租户 ID，与业务部门或子产品对应
  labels:
    domain: dev-tools
    owner: platform-team

spec:
  description: |
    自动 PR 代码评审 Agent：拉取 diff、检查代码风格 / 安全模式 / 测试覆盖，给出评论草稿并自动 commit 到 PR。

  # ---------- 租户级配置（多业务线平台关键）----------
  tenant_config:
    compliance_pack: null         # null | "hipaa" | "gdpr" | "sox" — 此 agent 不需合规
    pii_fields: []                # 此场景无 PII；医疗租户会列 ["patient.id_card", "phone", "mrn"]
    isolation_level: shared       # shared | dedicated_sandbox | dedicated_node
    audit_retention_days: 90      # 默认 90；HIPAA 合规会用 2555（7 年）
    data_residency: cn-shanghai   # M2 阶段强制；其他场景可空

  # ---------- 模板继承（可选）----------
  # extends: templates/dev-tools/code-review-base@1.2   # 引用模板包，下面字段会 merge

  # ---------- 模型 ----------
  model:
    provider: anthropic
    name: claude-sonnet-4-5
    temperature: 0.2
    # max_tokens: 16000       # 单次输出上限，含思考（思考 + 回答合计）；可省略，省略 = 厂商默认
    #                         # （Anthropic 省略时发 4096）。非 Anthropic 模型写 4096 会在加载时
    #                         # 归一成省略（旧默认值），确实要这个数请写 4095 / 4097
    fallback:                 # 主模型失败时按序降级；省略字段引擎自动注入推荐 chain（详见下文）
      - { provider: openai, name: gpt-4o }

  # ---------- 系统提示词 ----------
  # ⚠️ 重要：system_prompt.template 必须保持完全静态（不嵌入 session/turn 级动态值）
  # 任何随对话变化的内容（PR diff、当前日期、memory）都通过下面的 dynamic_context
  # 注入为独立 <system-reminder> HumanMessage，
  # 这样 system_prompt 永远命中 prefix cache（Anthropic 缓存折扣 0.1× → 长会话成本 ↓~10x）
  # 设计源自 DeerFlow DynamicContextMiddleware（详见 research/05-deerflow-deeper-scan.md）
  system_prompt:
    template: |
      你是资深代码评审员。审查 PR 时按以下顺序：
      1. 读取改动文件，理解变更意图
      2. 检查：命名规范 / 错误处理 / 边界条件 / 安全模式（XSS/SQL注入/越权）
      3. 检查测试覆盖（高风险改动必须有对应测试）
      4. 调用 security_auditor subagent 复核安全敏感改动
      5. 在 PR 上发表评论；只评论真问题，不刷存在感
      可用工具会按需通过 <system-reminder> 注入。
    # ❌ 不要这样写（破坏缓存）：
    # template: "PR diff：{{ context.pr.diff }}"

  # ---------- 动态上下文注入（保持 system_prompt 静态）----------
  dynamic_context:
    inject_memory: true                  # 注入 tenant/team memory（长期记忆，如团队代码规范偏好）
    inject_current_date: true            # 自动注入当前日期 + 午夜穿越检测
    custom_reminders:                    # 业务相关的动态注入
      - source: "$session.context.pr"
        template: "<pr>id={{ id }}, repo={{ repo }}, author={{ author }}</pr>"
      - source: "$session.context.repo_conventions"
        template: "<conventions>{{ value }}</conventions>"

  # ---------- 工具（声明式装配）----------
  # M0 工具声明用 `type` 判别字段（STREAM-E-DESIGN Mini-ADR E-14）。
  # http/mcp 的真实配置（URL allowlist / MCP server）是租户作用域，
  # 在 tenant_config 里，不在单 agent 的 manifest 内联。
  tools:
    # 1. 内置工具
    - type: builtin
      name: web_search
      config: { engine: tavily, max_results: 5 }

    # 2. HTTP 工具 — 启用开关；URL allowlist 在
    #    tenant_config.http_tool_allowlist（租户作用域，glob 模式）
    - type: http

    # 3. MCP 工具 — 启用开关；server 在 tenant_config.mcp_servers
    #    （租户作用域）。allow_tools 可选，留空 = 暴露全部 MCP 工具
    - type: mcp
      allow_tools: ["read_pr", "read_file", "post_comment"]

    # 4. python / subagent 工具 — M1-F（code 插槽 + Sub-Agent）。
    #    M0 声明这两类会被 manifest 校验拒绝（discriminated union 无此变体）。

  # ---------- 沙盒 ----------
  sandbox:
    runtime: gvisor                # gvisor | docker | none(纯 LLM 任务)
    image: Expert Work/python-sandbox:3.12-slim
    resources:
      cpu: "1.0"
      memory: "1Gi"
      pids: 256
      timeout_s: 600
    network:
      egress: proxy                # 必须经 Credential Proxy 出站
      allowlist:
        - "*.internal"
        - "api.anthropic.com"
    filesystem:
      readonly_root: true
      writable:
        - /workspace               # 临时挂载，会话结束清理
      mounts:
        - { type: tmpfs, target: /tmp, size: "256Mi" }

  # ---------- 记忆 ----------
  memory:
    short_term:
      type: langgraph_checkpoint
      window: 50                   # 保留最近 50 turn
    long_term:
      type: vector
      backend: pgvector            # 或 qdrant
      collection: "tenant_${tenant}_${name}"
      embed_model: text-embedding-3-large

  # ---------- 工作流（声明式 DAG）----------
  # 不写则使用默认 ReAct 循环
  workflow:
    type: react                    # react | plan_execute | custom
    max_iterations: 12
    early_stop:
      on_tool: ["finalize_review"]

  # ---------- 限流与策略 ----------
  policies:
    rate_limit:
      per_user_per_minute: 30
    pii:
      redact_in_logs: true        # 通用 redactor，按 tenant_config.pii_fields 配置
    safety:
      # 内置 input/output filter 按 compliance_pack 自动启用
      # 此场景不启用医疗/PII 过滤；客户敏感场景租户可启用 builtin/pii-egress
      input_filter: builtin/prompt-injection
      output_filter: null
    context_compression:           # 长会话上下文压缩；详见子系统 27
      enabled: true                # 默认 true；HIPAA 等场景引擎按 disable_for_compliance 强制关闭
      trigger_threshold_ratio: 0.8 # context_size / model_context_limit 超过此比例触发
      keep_recent_turns: 10        # 最近 K 轮原文不参与摘要
      summary_model:               # 摘要 LLM；省略则引擎默认走 haiku 类便宜模型
        provider: anthropic
        name: claude-haiku-4-5
      disable_for_compliance:      # 命中以下 compliance_pack 时禁用摘要（仅简单截断）
        - hipaa

  # ---------- Observability ----------
  observability:
    trace: opentelemetry
    log_level: info
    redact_fields: []             # 此 agent 无敏感字段
```

### 多业务线场景示例：合规模式启用

**同一引擎、不同租户配置不同合规级别**：

```yaml
# 医疗租户的某个 agent — 启用 HIPAA pack
metadata:
  tenant: medical-saas-tenant
spec:
  tenant_config:
    compliance_pack: hipaa                          # 启用 HIPAA pack
    pii_fields:                                     # 引擎按此自动 redact log/trace/event
      - "patient.id_card"
      - "patient.mrn"
      - "patient.phone"
      - "patient.dob"
    isolation_level: dedicated_sandbox              # 强隔离，不与其他租户共享 sandbox 实例
    audit_retention_days: 2555                      # HIPAA 7 年保留
    data_residency: cn-shanghai
  policies:
    safety:
      input_filter: builtin/healthcare-safety       # 医疗安全规则
      output_filter: builtin/pii-egress
```

```yaml
# HR 租户的某个 agent — 仅启用 GDPR
metadata:
  tenant: hr-saas-tenant
spec:
  tenant_config:
    compliance_pack: gdpr
    pii_fields: ["employee.id", "employee.salary", "employee.email"]
    isolation_level: shared
    audit_retention_days: 365
```

引擎根据 `compliance_pack` 自动应用对应的中间件、加密策略、保留 SLA、审计规则——业务侧 manifest **不需要写合规细节**。

---

## 20% 场景 — 带 Python 插槽（开发者扩展）

> **示例选择说明**：选**客服工单分类**这个跨业务线通用场景。任何 SaaS 平台都有工单流，分类后路由到不同处理队列；引擎不预设业务领域。

```yaml
# agents/ticket_classifier/manifest.yaml
apiVersion: expert_work.io/v1
kind: Agent
metadata: { name: ticket-classifier, version: "0.3.0", tenant: customer-success }

spec:
  tenant_config:
    compliance_pack: null
    pii_fields: ["customer.email", "customer.phone"]   # 工单可能含客户联系方式
    isolation_level: shared
    audit_retention_days: 365

  model: { provider: anthropic, name: claude-sonnet-4-5 }

  # ---------- 引用代码包 ----------
  code:
    package: ./agents/ticket_classifier         # Python 包路径
    requirements:                                # 仅声明依赖，引擎封装到 sandbox image
      - "scikit-learn==1.5.0"
      - "rapidfuzz==3.9.0"

  tools:
    - type: builtin
      name: web_search
    # 自定义 Python 工具（M1-F，code 插槽）：插槽 = "code.tools" 入口
    - type: python
      name: fetch_similar_tickets
      entrypoint: agents.ticket_classifier.tools:fetch_similar_tickets
      # 函数签名由 @tool 装饰器自动反射出 input/output schema
    - type: python
      name: ticket_classifier
      entrypoint: agents.ticket_classifier.tools:TicketClassifier   # 类也可

  # 自定义工作流：插槽 = "code.graph" 入口
  workflow:
    type: custom
    builder: agents.ticket_classifier.graph:build_graph
    # 引擎调用：build_graph(spec: AgentSpec, ctx: BuildContext) -> StateGraph

  # 自定义节点钩子：在固定位置注入逻辑
  hooks:
    pre_llm: agents.ticket_classifier.hooks:redact_pii         # tenant_config.pii_fields 之外的额外脱敏
    post_tool: agents.ticket_classifier.hooks:audit_tool_call
    on_error: agents.ticket_classifier.hooks:fallback_to_human

  sandbox:
    runtime: gvisor
    image_build:                                # 引擎自动构建定制镜像（缓存 SHA）
      base: Expert Work/python-sandbox:3.12-slim
      requirements_from: spec.code.requirements
      copy:
        - { src: agents/ticket_classifier, dst: /opt/app/agent }
```

### 对应 Python 插槽

```python
# agents/ticket_classifier/tools.py
from expert_work.sdk import tool, AgentContext

@tool
async def fetch_similar_tickets(query: str, top_k: int, ctx: AgentContext) -> list[dict]:
    """从 ticket 历史库召回最相似的 top_k 条工单（向量检索）。"""
    # 实现略
    ...

@tool
class TicketClassifier:
    """工单类别分类器（类也可作为 tool，使用 __call__ 方法）。
    输出 category ∈ {billing, technical, feedback, abuse, other}"""
    async def __call__(self, ticket_text: str, ctx: AgentContext) -> dict:
        ...
```

```python
# agents/ticket_classifier/graph.py
from langgraph.graph import StateGraph, END
from expert_work.sdk import AgentSpec, BuildContext, AgentState

def build_graph(spec: AgentSpec, ctx: BuildContext) -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("retrieve", ctx.tool_node("fetch_similar_tickets"))
    g.add_node("classify", ctx.tool_node("ticket_classifier"))
    g.add_node("draft_reply", ctx.llm_node(system=spec.system_prompt))

    g.add_edge("retrieve", "classify")
    g.add_conditional_edges("classify", route_by_category, {
        "billing":   "billing_queue",
        "technical": "technical_queue",
        "abuse":     END,            # 高敏感直接转人工
        "other":     "draft_reply",
    })
    return g

def route_by_category(state: AgentState) -> str:
    return state["classification"]["category"]
```

```python
# agents/ticket_classifier/hooks.py
from expert_work.sdk import HookContext

async def redact_pii(state, hook_ctx: HookContext):
    """LLM 调用前额外脱敏（tenant_config.pii_fields 已自动处理基础项）。"""
    state["messages"] = redact_extra_fields(state["messages"])
    return state

async def audit_tool_call(tool_call, result, hook_ctx: HookContext):
    """工具调用后写审计日志。"""
    await hook_ctx.event_log.append(
        event_type="audit",
        payload={"tool": tool_call.name, "result_hash": hash_redacted(result)}
    )
```

---

## Model fallback 推荐模板（引擎自动注入）

**默认行为**：如果 manifest 未声明 `model.fallback` 字段，引擎在加载阶段根据 `model.provider/name` **自动注入两级 fallback chain**。manifest 显式声明则 **完全覆盖** 默认；显式声明 `fallback: []` 表示 **明确禁用** 自动注入（不再走任何降级，主模型失败立即抛错）。

### 注入规则

| 层级 | 选取策略 | 目的 |
|------|---------|------|
| L1 | 同 provider 内便宜模型（容量降级） | 同一家 provider 区域故障概率小，配额多余；切到便宜模型可吸收瞬时压力，prefix cache 仍可能命中 |
| L2 | 跨 provider 等价模型（厂商降级） | 整家 provider 出问题（如 anthropic 全 region 5xx）时切到对家；prefix cache 必 miss，但保证可用性 |

### `model_equivalence_map`（引擎维护）

引擎在 `Expert Work/runtime/llm_fallback_map.yaml` 维护，管理员可热更新（每次更新写 audit `manifest:write` action 不适用 → 走 `quota:write` 类比的运维 audit；具体 action TBD）：

```yaml
# packages/expert-work-runtime/src/Expert Work/runtime/llm_fallback_map.yaml
anthropic/claude-sonnet-4-5:
  l1_cheap:    { provider: anthropic, name: claude-haiku-4-5 }
  l2_cross:    { provider: openai,    name: gpt-4o }

anthropic/claude-haiku-4-5:
  l1_cheap:    null                                  # 已是最便宜，跳过 L1
  l2_cross:    { provider: openai,    name: gpt-4o-mini }

openai/gpt-4o:
  l1_cheap:    { provider: openai,    name: gpt-4o-mini }
  l2_cross:    { provider: anthropic, name: claude-sonnet-4-5 }

openai/gpt-4o-mini:
  l1_cheap:    null
  l2_cross:    { provider: anthropic, name: claude-haiku-4-5 }

# self-hosted / azure 类似登记
```

新模型上线时管理员补这张表；CI 校验 manifest 的 primary model 必须在表中（`l1_cheap` / `l2_cross` 任一可空，但 primary key 必须存在）。

### 完整 YAML 示例

```yaml
# 例 1：未声明 fallback —— 引擎自动注入
spec:
  model:
    provider: anthropic
    name: claude-sonnet-4-5
    temperature: 0.2
  # 等效于运行时被注入为：
  # model:
  #   provider: anthropic
  #   name: claude-sonnet-4-5
  #   fallback:
  #     - { provider: anthropic, name: claude-haiku-4-5 }   # L1
  #     - { provider: openai,    name: gpt-4o }             # L2

# 例 2：显式声明 —— 覆盖默认
spec:
  model:
    provider: anthropic
    name: claude-sonnet-4-5
    fallback:
      - { provider: openai, name: gpt-4o }                  # 跳过 L1，直接跨 provider
      - { provider: self-hosted, name: qwen2-72b-instruct }

# 例 3：显式禁用 fallback
spec:
  model:
    provider: anthropic
    name: claude-sonnet-4-5
    fallback: []         # 主模型失败立即抛 LLMUnavailable，不降级
```

### 关键决策

- 自动注入仅在 **manifest 加载** 时一次性发生；写入 `AgentSpec` 后行为完全等价于 manifest 显式声明，运行时不再特殊处理
- 引擎暴露 `Expert Work inspect <agent>@<version> --model` 命令展示注入结果（运维可见 effective fallback chain）
- 注入的 model 必须满足同 `temperature` / `max_tokens` 范围（自动 clamp，不抬高）
- HIPAA / GDPR 租户：自动注入受 `data_residency` 约束（不会注入跨 region provider）
- fallback chain 长度上限 = 4（primary + 3 个 fallback）；超长 → 加载失败

### 与子系统的协同

详细的故障切换、重试、断路器逻辑见 [10 LLM Gateway § 5](./subsystems/10-llm-gateway.md)。本文件只规定 **manifest 层的字段语义** 与 **引擎自动注入策略**。

---

## 上下文压缩策略（`policies.context_compression`）

长会话超出模型 context window 的处置方式。详细机制（状态机、摘要 LLM、prefix cache 协同、失败回退）见 [27 上下文压缩](./subsystems/27-context-compression.md)。manifest 只声明 **何时触发** 与 **保留多少**。

### 字段说明

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enabled` | `bool` | `true` | 主开关；`false` → 完全禁用，超限直接 400 |
| `trigger_threshold_ratio` | `float` ∈ [0.5, 0.95] | `0.8` | 当前 context tokens / `model.max_context` 超过此值触发；与子系统 27 的 `token_trigger_ratio` 一一对应 |
| `keep_recent_turns` | `int` ∈ [2, 50] | `10` | 最近 K 轮 user/assistant message 保持原文，不进入摘要范围 |
| `summary_model` | `ModelSpec`（可选）| `null` | 摘要用 LLM；`null` 则引擎默认选 haiku 类便宜模型（与主模型 provider 同；走独立 quota bucket，参考子系统 27 § 5） |
| `disable_for_compliance` | `list[ComplianceLevel]` | `["hipaa"]` | 当 `tenant_config.compliance_pack` ∈ 此列表时，引擎强制关闭摘要（仅做简单截断），避免摘要 LLM 看到完整 PHI/PII |
| `pin_tools` | `list[str]` | `[]` | 永不被压缩的工具结果名（如 `read_patient_record`），保证关键引用原文不丢 |

### Pydantic schema

```python
from expert_work.types import ComplianceLevel

class ContextCompressionPolicy(BaseModel):
    enabled: bool = True
    trigger_threshold_ratio: float = Field(0.8, ge=0.5, le=0.95)
    keep_recent_turns: int = Field(10, ge=2, le=50)
    summary_model: ModelSpec | None = None
    disable_for_compliance: list[ComplianceLevel] = Field(default_factory=lambda: [ComplianceLevel.HIPAA])
    pin_tools: list[str] = []

class PolicySpec(BaseModel):
    rate_limit: RateLimitSpec | None = None
    pii: PIISpec | None = None
    safety: SafetySpec | None = None
    require_approval: list[ApprovalRule] = []        # 25 HITL
    context_compression: ContextCompressionPolicy = Field(default_factory=ContextCompressionPolicy)
```

### 完整 YAML 示例

```yaml
# 例 1：默认配置（绝大多数场景适用）
spec:
  policies:
    context_compression:
      enabled: true
      trigger_threshold_ratio: 0.8
      keep_recent_turns: 10

# 例 2：HIPAA 租户显式关闭摘要（即使 enabled=true，引擎也会强制走简单截断）
metadata:
  tenant: medical-saas-tenant
spec:
  tenant_config:
    compliance_pack: hipaa
  policies:
    context_compression:
      enabled: true
      keep_recent_turns: 20            # HIPAA 场景偏向保留更多原文，截断更激进
      disable_for_compliance: [hipaa]  # 显式列出，可读性

# 例 3：长会话深度对话场景（如代码 review），用强模型摘要 + pin 关键工具结果
spec:
  policies:
    context_compression:
      enabled: true
      trigger_threshold_ratio: 0.7     # 更早触发，避免在临界点 LLM 调用失败
      keep_recent_turns: 6
      summary_model:
        provider: anthropic
        name: claude-sonnet-4-5         # 用同级模型摘要保证质量
      pin_tools:
        - read_pr_diff                 # 关键参考资料原文不丢
        - get_review_history
```

### 关键决策

- `enabled=false` 与 `disable_for_compliance` 命中是 **两种不同语义**：前者完全禁用（超限即报错），后者降级为简单截断（仍可继续）
- 摘要 LLM 失败（API 报错 / redactor 拒绝）→ 引擎按 [子系统 27 § 5](./subsystems/27-context-compression.md) 状态机走 `FAILED_FALLBACK`（简单截断）或 `FAILED_HARD`（session 终止）
- `summary_model` 不可与主 `model` 同名（避免摘要 quota 与主对话 quota 互相抢占）；CI 校验
- `pin_tools` 列表中的工具必须在 `tools` 中声明；CI 校验

---

## 关键约束

| 约束 | 理由 |
|------|------|
| **YAML 只声明意图** | 引擎做翻译（如 `tools.http` → 自动生成 LangGraph ToolNode） |
| **Python 必须打成包** | 不允许 inline 代码字符串，避免审核盲点（YAML 内不能有任意代码）|
| **热更新粒度 = AgentSpec** | 旧 session 用旧 graph 跑完，新 session 用新 graph，零中断 |
| **静态校验**（lint 阶段）| tool 引用是否存在、subagent 循环依赖、secret 引用是否在 Vault、sandbox 资源是否超 tenant quota |
| **签名机制**（生产）| manifest 必须由 admin 签名（cosign），CI 自动校验签名 |
| **版本不可覆盖** | `name@version` 一旦发布即不可修改，要改就发新版本（`1.4.3`）|

---

## 多业务线平台关键设计：tenant_config + 模板包

Expert Work 是**业务无关引擎**——医疗、HR、客服、研发等场景共享一套引擎，差异化通过两个机制实现：

### 1. `tenant_config` 字段（每个 manifest 必填）

| 字段 | 类型 | 说明 |
|------|------|------|
| `compliance_pack` | `null \| "hipaa" \| "gdpr" \| "sox"` | 引擎据此自动启用对应中间件、加密、保留 SLA、审计规则 |
| `pii_fields` | `list[str]` | 通用 redactor 按此自动脱敏 log/trace/event；不同业务列不同字段 |
| `isolation_level` | `"shared" \| "dedicated_sandbox" \| "dedicated_node"` | 隔离强度；高合规租户强制 dedicated_sandbox |
| `audit_retention_days` | `int` | 审计日志保留天数；默认 90，HIPAA 用 2555 |
| `data_residency` | `str` | M2 强制；某些客户要求数据不出特定 region |

**引擎职责**：根据 `compliance_pack` 自动注入 middleware（如 hipaa pack 自动加上加密 audit、强制 dedicated_sandbox、PII 全字段强 redact），manifest 业务侧**不需要写合规细节**。

### 2. `extends` 模板继承（DRY）

```yaml
# 模板包：templates/customer-service/intake-base.yaml（公司内部库）
spec:
  model: { provider: anthropic, name: claude-sonnet-4-5 }
  policies:
    safety:
      input_filter: builtin/customer-service-guardrails
      output_filter: builtin/pii-egress
  tools:
    - builtin: web_search
    - mcp: { name: crm-mcp, ... }
```

```yaml
# 业务 agent：基于模板裁剪
metadata: { name: tier1-support-agent, tenant: ops }
spec:
  extends: templates/customer-service/intake-base@1.2
  # 下面字段 merge / override 模板
  tools:
    - mcp: { name: ticket-mcp, ... }     # 追加业务专属工具
  workflow: { type: react, max_iterations: 8 }
```

**模板包目录**（公司维护，`expert-work-cli` 可发布版本）：
- `templates/dev-tools/` — 代码 review、测试生成、文档总结
- `templates/customer-service/` — 工单分类、客户回复
- `templates/medical/` — 医疗场景预设（启用 hipaa pack、医疗 guardrails、临床术语词典）
- `templates/hr/` — HR 工作流预设
- 业务团队按需引用，不重复造每个 agent

---

## AgentSpec Pydantic Schema（核心字段）

```python
# packages/expert-work-protocol/src/Expert Work/protocol/agent_spec.py
from pydantic import BaseModel, Field
from typing import Literal, Union

class ModelSpec(BaseModel):
    provider: Literal["anthropic", "openai", "azure", "self-hosted"]
    name: str
    temperature: float = 0.2
    max_tokens: int | None = None  # 含思考;None = 厂商默认(Anthropic 发 4096)
    fallback: list["ModelSpec"] = []

class ToolBuiltin(BaseModel):
    builtin: str
    config: dict = {}

class ToolMCP(BaseModel):
    mcp: "MCPToolDef"

class ToolHTTP(BaseModel):
    http: "HTTPToolDef"

class ToolPython(BaseModel):
    python: "PythonToolDef"

class ToolSubagent(BaseModel):
    subagent: "SubagentToolDef"

ToolDef = Union[ToolBuiltin, ToolMCP, ToolHTTP, ToolPython, ToolSubagent]

class SandboxSpec(BaseModel):
    runtime: Literal["gvisor", "docker", "none"] = "gvisor"
    image: str | None = None
    image_build: "ImageBuildSpec | None" = None
    resources: "ResourceSpec"
    network: "NetworkSpec"
    filesystem: "FilesystemSpec"

class WorkflowSpec(BaseModel):
    type: Literal["react", "plan_execute", "custom"] = "react"
    max_iterations: int = 12
    early_stop: dict = {}
    builder: str | None = None    # 仅 type=custom 时

class AgentSpec(BaseModel):
    api_version: str = Field(alias="apiVersion")
    kind: Literal["Agent"]
    metadata: "AgentMetadata"
    spec: "AgentSpecBody"

class AgentSpecBody(BaseModel):
    description: str = ""
    extends: str | None = None              # 模板包引用：templates/foo/bar@1.2
    tenant_config: "TenantConfig"           # 多业务线关键字段
    model: ModelSpec
    system_prompt: "SystemPromptSpec"
    dynamic_context: "DynamicContextSpec" = "DynamicContextSpec()"
    tools: list[ToolDef] = []
    sandbox: SandboxSpec
    memory: "MemorySpec"
    workflow: WorkflowSpec = WorkflowSpec()
    policies: "PolicySpec" = "PolicySpec()"
    code: "CodePackageSpec | None" = None
    hooks: dict[str, str] = {}
    observability: "ObservabilitySpec" = "ObservabilitySpec()"

class TenantConfig(BaseModel):
    compliance_pack: Literal["hipaa", "gdpr", "sox"] | None = None
    pii_fields: list[str] = []
    isolation_level: Literal["shared", "dedicated_sandbox", "dedicated_node"] = "shared"
    audit_retention_days: int = 90
    data_residency: str | None = None
```

---

## 静态校验规则（lint 阶段必跑）

1. **secret 引用**：所有 `!secret xxx` 必须在 Vault 已登记
2. **tool 引用**：subagent 引用的其他 agent 必须存在且版本可解析
3. **subagent 循环**：递归检测调用图，禁循环
4. **sandbox quota**：`resources.{cpu,memory}` 不能超过该 tenant 配额
5. **MCP 白名单**：`mcp.allow_tools` 列出的工具必须在该 MCP server 公布的工具列表中
6. **Python 包**：`code.package` 路径存在，`requirements` 全部可在镜像构建时安装
7. **网络 allowlist**：不能为 `["*"]`（必须明确列出域名）
8. **fallback 模型**：fallback chain 不能形成循环
