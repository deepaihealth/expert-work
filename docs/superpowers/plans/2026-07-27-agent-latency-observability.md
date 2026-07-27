# Agent 延迟可观测性 + 连接复用（一期）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent 首字延迟从「看不见」变成「分段可见」，同时消掉每次 HTTP 调用的 TLS 握手，并用可复用的 bench 脚本把差值量出来。

**Architecture:** 三层。orchestrator 侧在入口链（`memory_recall` / `workspace_ingest` / context gates）补 8 个 OTel span，并把首字时刻做成带 `source` label 的直方图；control-plane 的 trace facade 把这些 span 归一成带中文标签和 `group` 字段的行；admin-ui 在 TraceView 顶部按 `group` 渲染一条 pre-first-token 分解条。连接复用是一个挂在 control-plane lifespan 的进程级 `httpx.AsyncClient`，通过各 client 类新增的 `http` 字段注入，字段为 `None` 时行为与改造前逐字节一致。

**Tech Stack:** Python 3.13 / LangGraph / OpenTelemetry / prometheus_client / httpx / FastAPI；前端 React + antd + vitest。

## Global Constraints

以下约束对每个 task 都生效，task 内不再重复。

- **span 命名契约**：`expert_work.{component}.{action}`，`component` 必须是 `ExpertWorkComponent` 枚举成员（`packages/expert-work-common/src/expert_work/common/observability/tracing.py:39`）。新增 action 不需要改枚举，新增 component 才需要。
- **直方图命名契约**：`expert_work_histogram` 强制 `_seconds` 后缀（`metrics.py:135`），毫秒名会在 import 时抛 `MetricNamingError`。
- **不改任何默认值**：`verify_reads`、`rewrite_reads`、`HOLD_CHARS`、`MAX_TOOL_WORKERS`、`MAX_SUBAGENT_DEPTH` 一律不动。二期再谈。
- **连接复用的兼容契约**：每个 client 类新增的 `http: httpx.AsyncClient | None = None` 默认 `None`，此时**必须**退回原本的 per-call `async with httpx.AsyncClient(...)` 路径。既有 `transport` 字段原样保留。
- **CI 命令**（合并前本地跑同款，范围要一致）：
  - `uv run ruff check` —— **无路径参数，跑全库含 tests**
  - `uv run ruff format --check` —— **CI 里是独立于上一条的一步**（`.github/workflows/ci.yml:40-41`）。`ruff check` 绿不代表这条绿：改动让某行的缩进变深/变浅后，原本需要换行包裹的调用可能变成一行放得下，format 会要求折叠它。**凡是给已有代码加 `with` 块（缩进整体变化）的 task 必跑这条。**
  - `uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src` —— **不含 control-plane**，control-plane 的类型问题 CI 不抓，本地也要自己看
  - `uv run pytest -v -m "not integration" --timeout=120 --timeout-method=thread`
  - `uv run pytest -v -m integration`（需 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`）
  - 前端：`node "$(dirname "$(which pnpm)")/../lib/node_modules/corepack/dist/pnpm.js" -C apps/admin-ui <cmd>`（本地 corepack shim 会被 SIGKILL）
- **编辑器诊断在本仓库大面积 stale**（`React refers to a UMD global`、`toBeInTheDocument does not exist`、`declared but never read` 都是假阳）。**只有真跑 `tsc -b --noEmit` 和 `vitest run` 的输出算数**，不要根据编辑器报错改代码。
- **CodeQL 会卡合并**：`py/log-injection`（request 派生值进 `logger`，包括 `extra={...}` 里）、`py/side-effect-in-assert`、以及 `await 裸名` 报 ineffectual-statement。新代码避开这三类。
- **已知测试噪音**（非回归，单独跑是绿的）：`rls_detect` 顺序、pgbouncer、`eval_engine_live`（`No module named 'tools'`）、`pg_restore_drill`、orchestrator cross-talk。
- **i18n 新键先 grep 是否撞既有** —— 同一 object 内重复键会被 esbuild 静默覆盖，不报错。

---

## File Structure

**新建**

| 文件 | 职责 |
|---|---|
| `services/orchestrator/tests/test_entry_chain_spans.py` | 入口链 8 个 span 的发射断言 + `TRACED_SPANS` parity |
| `services/orchestrator/tests/test_first_output_metric.py` | `first_output_seconds` 两条 source 路径 |
| `tools/bench/entry_latency.py` | 取数脚本：跑 N 轮 → 抽 span 树 → 出 median/p95 |
| `tools/bench/baselines/` | 基线 YAML（照 `tools/eval/baselines/` 形状） |
| `apps/admin-ui/src/pages/agent_detail/playground/entry_breakdown.ts` | 纯函数：从 `TraceSpan[]` 算分解条的段 |
| `apps/admin-ui/src/pages/agent_detail/playground/EntryBreakdown.tsx` | 分解条组件 |
| `apps/admin-ui/src/pages/agent_detail/playground/__tests__/entry_breakdown.test.ts` | 分段算法测试 |

**修改**

| 文件 | 改什么 |
|---|---|
| `packages/.../observability/tracing.py` | 加 `TRACED_SPANS` 单源 |
| `packages/.../observability/__init__.py` | 导出 `TRACED_SPANS` |
| `services/orchestrator/.../graph_builder/memory.py` | 6 个 span（recall 父 + 5 子） |
| `services/orchestrator/.../graph_builder/workspace_ingest.py` | 1 个 span |
| `services/orchestrator/.../graph_builder/builder.py` | `context_gates` span |
| `services/orchestrator/src/orchestrator/sse.py` | `first_output_seconds` + 老指标改名 |
| `services/control-plane/.../runtime.py` | 两个 attribute + embedder/reranker 的 `http` 注入 |
| `services/control-plane/.../api/trace_facade.py` | `_SPAN_LABELS` + `group` 字段 |
| `services/control-plane/.../app.py` | lifespan 建共享 `AsyncClient` |
| `services/orchestrator/.../llm/providers/{openai,anthropic}.py` | `http` 字段 |
| `services/orchestrator/.../llm/{embedder,rerank}.py` | `http` 字段 |
| `services/orchestrator/.../tools/{web_search,sandbox}.py` | `http` 字段 |
| `services/orchestrator/src/orchestrator/agent_factory.py` | `http_client` kwarg 串到 provider |
| `apps/admin-ui/src/api/trace_facade.ts` | `TraceSpan.group` |
| `apps/admin-ui/.../playground/TraceView.tsx` | 挂分解条 + `group` 配色 |
| `apps/admin-ui/src/i18n/locales/{zh-CN,en}.ts` | 分解条文案 |

## 交付波次

`spec §1` 的五行在实施上细化成 6 个 task（span 发射与 facade 渲染拆开，reviewer 可独立评判）。

| 波 | task | 并行安全性 |
|---|---|---|
| 1 | T1 span 发射、T3 指标 | 文件不重叠（T1 动 memory/ingest/builder/tracing/runtime，T3 只动 sse.py） |
| 2 | T2 facade | 依赖 T1 的 span 名 |
| 3 | T4 bench+基线、T6 前端 | T4 依赖 T1/T2/T3；T6 依赖 T2 的 `group` |
| 4 | T5 连接池 | **必须**在 T4 跑完基线后 cut，且与 T1 同改 `runtime.py`，故不与 T1 并行 |

每个并行 worktree 从 **main** cut，dispatch 第一步是 `git merge --ff-only <上一波已合分支>`。

---

## Task 1: 入口链 span 发射 + 单源契约

**Files:**
- Modify: `packages/expert-work-common/src/expert_work/common/observability/tracing.py`
- Modify: `packages/expert-work-common/src/expert_work/common/observability/__init__.py`
- Modify: `services/orchestrator/src/orchestrator/graph_builder/memory.py:540-648`
- Modify: `services/orchestrator/src/orchestrator/graph_builder/workspace_ingest.py:87-122`
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:617-632`
- Modify: `services/control-plane/src/control_plane/runtime.py:810-823, 860-872`
- Test: `services/orchestrator/tests/test_entry_chain_spans.py`

**Interfaces:**
- Produces: `TRACED_SPANS: frozenset[str]` in common —— Task 2 的 facade parity 测消费它。
- Produces: 8 个 span 名（见 Step 1 的表）—— Task 2 给每个配中文标签，Task 4 的 bench 按名抽取。

- [ ] **Step 1: 在 common 加单源，先写 parity 测**

新建 `services/orchestrator/tests/test_entry_chain_spans.py`：

```python
"""入口链 span 契约测试 —— 一期 Task 1。

``TRACED_SPANS`` 是非 LLM 追踪 span 名的单源。这里断言每个名字都真的
被发射（对着 InMemorySpanExporter），以及集合本身没有漏项。姊妹契约
``LLM_SPAN_PURPOSES`` 由 ``test_aux_llm_spans.py`` 守。
"""
from expert_work.common.observability import TRACED_SPANS

EXPECTED = {
    "expert_work.memory.recall",
    "expert_work.memory.resolve_mode",
    "expert_work.memory.embed",
    "expert_work.memory.retrieve",
    "expert_work.memory.rerank",
    "expert_work.memory.bump_access",
    "expert_work.orchestrator.workspace_ingest",
    "expert_work.orchestrator.context_gates",
}


def test_traced_spans_covers_every_entry_chain_span() -> None:
    assert TRACED_SPANS == EXPECTED
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest services/orchestrator/tests/test_entry_chain_spans.py -v
```
Expected: FAIL — `ImportError: cannot import name 'TRACED_SPANS'`

- [ ] **Step 3: 在 tracing.py 加单源**

在 `LLM_SPAN_PURPOSES`（`tracing.py:84`）那个 dict 之后插入：

```python
#: SINGLE SOURCE OF TRUTH for the non-LLM traced spans the trace facade
#: labels. Sibling of :data:`LLM_SPAN_PURPOSES`, deliberately separate: that
#: one's values are *LLM call purposes*, and these spans wrap DB / HTTP /
#: pure-CPU work, not LLM calls. The control-plane facade keeps a Chinese
#: label per name (``_SPAN_LABELS``) and its tests assert parity against this
#: set — without the single source, renaming a span here would silently make
#: the console fall back to the raw English name (``_classify``'s default
#: branch) with nothing failing CI.
TRACED_SPANS: frozenset[str] = frozenset(
    {
        _llm_span_name(ExpertWorkComponent.MEMORY, "recall"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "resolve_mode"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "embed"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "retrieve"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "rerank"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "bump_access"),
        _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "workspace_ingest"),
        _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "context_gates"),
    }
)
```

`_llm_span_name` 的名字带 `llm` 只是历史包袱（它就是 `f"expert_work.{component}.{action}"`），复用它保证两个契约的名字构造方式一致。如果觉得名字误导，可在本 task 内加一个 `_span_name = _llm_span_name` 别名，但**不要**改 `_llm_span_name` 本身的名字（`LLM_SPAN_PURPOSES` 的 11 个调用点会一起动，扩大 diff）。

`packages/expert-work-common/src/expert_work/common/observability/__init__.py` 里，照 `LLM_SPAN_PURPOSES`（`:63`、`:80`）的两处写法加导出：

```python
    TRACED_SPANS as TRACED_SPANS,
```
以及 `__all__` 里加 `"TRACED_SPANS",`。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest services/orchestrator/tests/test_entry_chain_spans.py -v
```
Expected: PASS

- [ ] **Step 5: 写 span 发射的断言测试（先失败）**

在同一测试文件追加。参照 `services/orchestrator/tests/test_aux_llm_spans.py` 的 `InMemorySpanExporter` fixture 写法（读那个文件的 fixture 部分，照抄 provider 装配）：

```python
async def test_memory_recall_emits_the_full_entry_chain(span_exporter, ...) -> None:
    """一次 recall 应发射 recall 父 span + resolve_mode/embed/retrieve/bump_access
    四个子 span。rerank / verify 依配置可选，这里不装配它们。"""
    node = make_memory_recall_node(...)   # 照既有 memory 测试的装配
    await node(state, config)

    names = {s.name for s in span_exporter.get_finished_spans()}
    assert "expert_work.memory.recall" in names
    assert "expert_work.memory.resolve_mode" in names
    assert "expert_work.memory.embed" in names
    assert "expert_work.memory.retrieve" in names
    assert "expert_work.memory.bump_access" in names


async def test_recall_children_nest_under_the_recall_span(span_exporter, ...) -> None:
    """子 span 必须真的挂在 recall 父 span 下 —— 平铺的话瀑布图读不出层次。"""
    node = make_memory_recall_node(...)
    await node(state, config)

    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    parent = spans["expert_work.memory.recall"]
    child = spans["expert_work.memory.embed"]
    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id
```

第二个测试是**这个 task 的命门** —— 只断言「名字出现了」的话，把 8 个 span 全平铺发射也能过测，但瀑布图上就是 8 条并列的根，读不出「recall 里面 embed 占了多少」。

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest services/orchestrator/tests/test_entry_chain_spans.py -v
```
Expected: FAIL — `KeyError: 'expert_work.memory.recall'`

- [ ] **Step 7: memory.py 布点**

`memory.py:540` 的 `memory_recall_node`，用父 span 包住整个函数体（早退分支也在里面），子 span 包住每段：

```python
    async def memory_recall_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        tenant_id = configurable_uuid(config, "tenant_id")
        user_id = configurable_uuid(config, "user_id")
        if tenant_id is None or user_id is None:
            return {}
        task = _last_human_text(list(state["messages"]))
        if not task:
            return {}
        # 一期 Task 1 —— 整段召回是入口链上最大的一块，父 span 让瀑布图
        # 能先看到总量再下钻。放在两处 no-op 早退之后:没有 tenant/user
        # 或没有 task 的 run 根本没做召回,发一个 0ms 的空 span 只是噪音。
        with expert_work_span(ExpertWorkComponent.MEMORY, "recall"):
            with expert_work_span(ExpertWorkComponent.MEMORY, "resolve_mode"):
                mode = await _resolve_memory_recall_mode(
                    tenant_id=tenant_id, tenant_config_store=tenant_config_store
                )
            recall_limit = max(top_k, _MEMORY_RECALL_WIDE_LIMIT)
            try:
                search_text = task
                if rewrite_query and rewriter is not None:
                    search_text = await _rewrite_query(
                        llm_caller=rewriter, task=task, token=token
                    )
                with expert_work_span(ExpertWorkComponent.MEMORY, "embed"):
                    vectors = await token.run_cancellable(
                        embedder.embed([search_text], tenant_id=tenant_id)
                    )
                with expert_work_span(ExpertWorkComponent.MEMORY, "retrieve"):
                    memories = await memory_store.retrieve(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        query_embedding=vectors[0],
                        query_text=search_text if mode == "hybrid" else None,
                        agent_name=agent_name,
                        limit=recall_limit,
                    )
                # …abstain 分支原样不动…
                if reranker is not None and memories:
                    with expert_work_span(ExpertWorkComponent.MEMORY, "rerank"):
                        memories = await _rerank_memories(
                            reranker=reranker,
                            query=task,
                            candidates=memories,
                            top_k=len(memories),
                            tenant_id=tenant_id,
                            token=token,
                        )
                # …mmr / verify 原样不动（verify 自带 span）…
            except RunCancelledError:
                raise
            except Exception:
                logger.warning(
                    "memory.recall_failed — continuing without memories", exc_info=True
                )
                record_memory_retrieval(mode=mode, result="miss")
                return {}
            record_memory_retrieval(mode=mode, result="hit" if memories else "miss")
            if memories:
                with expert_work_span(ExpertWorkComponent.MEMORY, "bump_access"):
                    try:
                        await memory_store.bump_access(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            ids=[m.id for m in memories],
                        )
                    except RunCancelledError:
                        raise
                    except Exception:
                        logger.warning("memory.bump_access_failed", exc_info=True)
            redacted = [_redact_memory(m) for m in memories]
            logger.info("memory.recall count=%d mode=%s", len(redacted), mode)
            return {"recalled_memories": redacted}
```

注意 `_rewrite_query` 已有自己的 `memory.query_rewrite` span（`memory.py:471`），不要再包一层。

- [ ] **Step 8: workspace_ingest.py 布点**

`workspace_ingest.py:87`，包住 `tenant_id` 检查之后的全部：

```python
        if tenant_id is None:
            return {}
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "workspace_ingest"):
            ctx = ToolContext(
                ...
            )
            # …函数体其余部分整体缩进一层…
            return {"plan": candidate}
```

文件顶部加 import：

```python
from expert_work.common.observability import ExpertWorkComponent, expert_work_span
```

- [ ] **Step 9: builder.py 的 context_gates 布点**

`builder.py:617-632`，把 prune 和 window 两道门包进一个 span（compress 自带 `orchestrator.compress` span，不要包进来 —— 它是 LLM 调用，已在 `LLM_SPAN_PURPOSES` 里）：

```python
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "context_gates"):
            if tool_result_pruner is not None:
                messages = tool_result_pruner.apply(messages).messages
            if working_window is not None:
                trim = working_window.apply(messages)
                messages = trim.messages
                _cm_working_window_total.labels(
                    outcome="trimmed" if trim.dropped_turns else "noop"
                ).inc()
                _cm_working_window_dropped_turns.set(trim.dropped_turns)
```

`builder.py` 已经 import 了 `expert_work_span`（`:87`）。

- [ ] **Step 10: 跑测试确认通过**

```bash
uv run pytest services/orchestrator/tests/test_entry_chain_spans.py -v
```
Expected: PASS

- [ ] **Step 11: 在 ResolvingEmbedder / ResolvingReranker 露出隐藏 I/O**

`runtime.py:810` 的 `ResolvingEmbedder.embed`。它跑在 orchestrator 的 `memory.embed` span 内，所以 `get_current_span()` 拿到的就是那个 span：

```python
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        if not texts:
            return []
        # 一期 Task 1 —— 每次 embed 都重解一次凭据(DB 读 + vault 读)。这两段
        # 挂到调用方的 ``memory.embed`` span 上而不是各开一个子 span:它们是
        # 同一次调用的内部构成,单列会让瀑布图多两行毫秒级噪音。二期若做
        # 凭据缓存,这两个数字就是收益的度量。
        t0 = time.monotonic()
        secret_ref = await self.resolver.resolve_provider(
            tenant_id=tenant_id, provider=self.provider
        )
        t1 = time.monotonic()
        api_key = await self.secret_store.get(parse_secret_ref(secret_ref))
        t2 = time.monotonic()
        span = trace.get_current_span()
        span.set_attribute("resolve_ms", round((t1 - t0) * 1000))
        span.set_attribute("secret_ms", round((t2 - t1) * 1000))
        delegate = OpenAICompatibleEmbedder(
            client=HTTPEmbeddingClient(api_key=api_key), model=self.model
        )
        return await delegate.embed(texts, tenant_id=tenant_id)
```

`ResolvingReranker.rerank`（`runtime.py:860` 附近）同款处理，注意它的 `secret_store.get` 只在 DashScope 分支里（`:864`），LLM 分支走 `build_llm_router`，所以两个分支各自打点。

`trace.get_current_span()` 在没有活跃 span 时返回 `INVALID_SPAN`，`set_attribute` 是安全 no-op —— 单测和 eval CLI 不受影响，不需要额外守卫。

顶部加 `import time` 和 `from opentelemetry import trace`（检查是否已 import）。

- [ ] **Step 12: 全量校验**

```bash
uv run pytest -v -m "not integration" --timeout=120 --timeout-method=thread -k "memory or span or trace or workspace_ingest"
uv run ruff check
uv run mypy packages services/orchestrator/src
```
Expected: 全 PASS。注意 mypy 范围不含 control-plane，`runtime.py` 的改动要自己看一眼类型。

- [ ] **Step 13: Commit**

```bash
git add packages/expert-work-common services/orchestrator services/control-plane/src/control_plane/runtime.py
git commit -m "feat(observability): 入口链 8 个 span + TRACED_SPANS 单源"
```

---

## Task 2: trace facade 中文标签 + group 字段

**Files:**
- Modify: `services/control-plane/src/control_plane/api/trace_facade.py:45-66, 300-410`
- Test: `services/control-plane/tests/test_trace_facade_normalize.py`

**Interfaces:**
- Consumes: `TRACED_SPANS`（Task 1 produces）。
- Produces: `TraceSpan.group: str | None`，值域 `"entry" | None` —— Task 6 的前端按它上色。

- [ ] **Step 1: 写 parity 测 + group 测（先失败）**

`services/control-plane/tests/test_trace_facade_normalize.py` 追加。照既有的 `_LLM_LABELS` parity 测（`:152-154`）的形状：

```python
def test_span_labels_cover_every_traced_span() -> None:
    """非 LLM span 的中文标签必须覆盖 TRACED_SPANS 的每一项 —— 漏一个就会
    静默退回 _classify 的裸英文名 fallback,不炸 CI。"""
    from expert_work.common.observability import TRACED_SPANS

    from control_plane.api.trace_facade import _SPAN_LABELS

    assert set(_SPAN_LABELS) == set(TRACED_SPANS)


def test_entry_chain_spans_carry_the_entry_group() -> None:
    """入口链 span 带 group="entry",前端据此上色并算分解条。"""
    spans = _normalize(_fake_observations([
        _obs(id="a", name="expert_work.memory.recall", obs_type="SPAN"),
        _obs(id="b", name="expert_work.orchestrator.tool_call", obs_type="SPAN"),
    ]))
    by_label = {s.id: s for s in spans}
    assert by_label["a"].group == "entry"
    assert by_label["a"].label == "记忆召回"
    assert by_label["b"].group is None
```

第二个测试里的 `_normalize` / `_obs` 用该测试文件既有的构造 helper（读文件顶部照抄）。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest services/control-plane/tests/test_trace_facade_normalize.py -v -k "span_labels or entry_group"
```
Expected: FAIL — `ImportError: cannot import name '_SPAN_LABELS'`

- [ ] **Step 3: 加标签表**

`trace_facade.py`，紧跟 `_LLM_LABELS`（`:319-331`）之后：

```python
#: Human labels for the non-LLM entry-chain spans. Sibling of
#: :data:`_LLM_LABELS`; keys kept in parity with common's ``TRACED_SPANS`` by
#: the facade tests, so a span renamed in the orchestrator without a label
#: here fails CI rather than silently rendering its raw English name.
_SPAN_LABELS: dict[str, str] = {
    "expert_work.memory.recall": "记忆召回",
    "expert_work.memory.resolve_mode": "读取召回配置",
    "expert_work.memory.embed": "向量化",
    "expert_work.memory.retrieve": "向量检索",
    "expert_work.memory.rerank": "记忆重排",
    "expert_work.memory.bump_access": "回写访问计数",
    "expert_work.orchestrator.workspace_ingest": "工作区摄取",
    "expert_work.orchestrator.context_gates": "上下文门",
}
```

`memory.rerank` 标「记忆重排」，跟 `_LLM_LABELS` 里已有的 `orchestrator.rerank`「文档重排」（知识库那条）区分开。

- [ ] **Step 4: `TraceSpan` 加 group 字段**

`trace_facade.py:45` 的 dataclass 末尾加：

```python
    #: Which functional stage this span belongs to, for the console's colour
    #: grouping and the pre-first-token breakdown bar. "entry" = the entry
    #: chain (recall / ingest / context gates); None = everything else.
    #: Deliberately NOT folded into ``purpose``: that field is locked to
    #: ``LLM_SPAN_PURPOSES`` by a parity test and means "LLM call intent",
    #: so a non-LLM span carrying purpose="recall" would mislead.
    group: str | None = None
```

放在 `purpose` 之后并给默认值 —— 现有的所有构造点（`:300` 附近那个 `_ParsedObs` → `TraceSpan` 的转换）不需要每处都改。

- [ ] **Step 5: 在 `_classify` 里认标签，并填 group**

`_classify`（`:404`）改成：

```python
def _classify(obs_type: str, name: str) -> tuple[str, str, str | None]:
    """Map an observation's raw ``type``/``name`` to (kind, label, group)."""
    if obs_type == "GENERATION":
        return "llm", "LLM 调用", None
    if ".tool_call" in name:
        return "tool", "工具调用", None
    if ".session.run" in name:
        return "session", "会话运行", None
    entry_label = _SPAN_LABELS.get(name)
    if entry_label is not None:
        return "span", entry_label, "entry"
    return "span", _clean_label(name), None
```

返回值从 2-tuple 变 3-tuple，调用点要一起改（grep `_classify(` 找全）。

- [ ] **Step 6: 跑测试确认通过**

```bash
uv run pytest services/control-plane/tests/test_trace_facade_normalize.py -v
```
Expected: PASS（含既有测试不回归）

- [ ] **Step 7: 全量校验 + Commit**

```bash
uv run pytest -v -m "not integration" -k "trace_facade"
uv run ruff check
git add services/control-plane
git commit -m "feat(control-plane): trace facade 给入口链 span 配中文标签 + group 字段"
```

---

## Task 3: first_output 指标

**Files:**
- Modify: `services/orchestrator/src/orchestrator/sse.py:113-117, 379-382, 437-475`
- Test: `services/orchestrator/tests/test_first_output_metric.py`

**Interfaces:**
- Produces: `expert_work_first_output_seconds{source}` —— Task 4 的 bench 不直接读它（bench 走 trace），但验收看它。

**与 Task 1 并行安全**：只动 `sse.py`，Task 1 不碰这个文件。

- [ ] **Step 1: 写测试（先失败）**

新建 `services/orchestrator/tests/test_first_output_metric.py`：

```python
"""first_output_seconds 的两条 source 路径 —— 一期 Task 3。

判断准则是"用户第一次看到内容"。有 token 流时走 token 帧;judge 开启 /
cache 命中 / provider 不流式这三类 run 一个 token 帧都没有,必须由第一个
**agent 节点**的 updates 帧兜底,否则最慢的那批 run 全部落在盲区。
"""


async def test_token_frame_records_source_token(...) -> None:
    """有 token 流时,第一帧 token 打 source="token"。"""
    before = _histogram_count("expert_work_first_output_seconds", source="token")
    await run_agent(...)  # 装配一个会发 token 帧的 graph
    assert _histogram_count("expert_work_first_output_seconds", source="token") == before + 1


async def test_agent_updates_frame_records_source_node_when_no_tokens(...) -> None:
    """judge-on 这类无 token 流的 run,由 agent 节点的 updates 帧兜底。"""
    before = _histogram_count("expert_work_first_output_seconds", source="node")
    await run_agent(...)  # 装配一个 token sink 为 None 的 graph
    assert _histogram_count("expert_work_first_output_seconds", source="node") == before + 1


async def test_recall_chunk_does_not_count_as_first_output(...) -> None:
    """入口链节点的 updates 帧不算首字 —— 用户看不到 recall 的输出。

    这是本 task 的命门:沿用现有的 first_chunk_seen(sse.py:470,认任意
    第一个 chunk)会把 memory_recall 完成的时刻当成首字,数字比真实值早
    好几秒,优化前后的对比会完全失真。
    """
    before = _histogram_count("expert_work_first_output_seconds", source="node")
    # graph 装配成 memory_recall → agent,且无 token 流
    await run_agent(...)
    # 只记一次,且记的是 agent 那帧而非 recall 那帧
    assert _histogram_count("expert_work_first_output_seconds", source="node") == before + 1


async def test_records_at_most_once_per_run(...) -> None:
    """token 帧记过之后,后续 agent updates 帧不再重复记。"""
    before_t = _histogram_count("expert_work_first_output_seconds", source="token")
    before_n = _histogram_count("expert_work_first_output_seconds", source="node")
    await run_agent(...)  # 有 token 流的多步 run
    assert _histogram_count("expert_work_first_output_seconds", source="token") == before_t + 1
    assert _histogram_count("expert_work_first_output_seconds", source="node") == before_n
```

`_histogram_count` 用 `prometheus_client` 的 `REGISTRY.get_sample_value("expert_work_first_output_seconds_count", {"source": source})`，注意 `None` 要当 0 处理（指标第一次被观测前样本不存在）。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest services/orchestrator/tests/test_first_output_metric.py -v
```
Expected: FAIL — 指标不存在，`get_sample_value` 返回 `None`

- [ ] **Step 3: 定义指标 + 老指标改名**

`sse.py:113`：

```python
# 一期 Task 3 —— 老名字叫 ttft 但测的是"第一个图节点完成",有 memory_recall
# 的 run 测到的是召回结束,不是首字。改名说实话,真首字用下面的
# first_output_seconds。
_session_first_node_seconds = expert_work_histogram(
    "expert_work_session_first_node_seconds",
    "Seconds from RUNNING to the first graph-node update chunk.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

#: 用户第一次看到内容的时刻。``source="token"`` 走流式首帧;``source="node"``
#: 是无 token 流的 run(output judge 开启 / LLM cache 命中 / provider 不支持
#: 流式)的兜底,取第一个 **agent** 节点的 updates 帧。两条路径互斥、先到先得。
#: 不做兜底的话最慢的那批(judge-on)完全不进直方图,是幸存者偏差。
_first_output_seconds = expert_work_histogram(
    "expert_work_first_output_seconds",
    "Seconds from RUNNING to the first content the user can see.",
    ("source",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)
```

`:472` 和 `:474` 两处 `_session_ttft_seconds.observe(ttft)` 改成 `_session_first_node_seconds.observe(ttft)`。grep 确认没有别处引用这个名字。

- [ ] **Step 4: token 路径打点**

`sse.py:379` 的 `_publish_token` 改成：

```python
    first_output_recorded = False

    async def _publish_token(frame: Any) -> None:
        nonlocal first_output_recorded
        # Live-only: token frames are provisional; do NOT mirror to the event
        # store (the authoritative ``updates`` frame is what replays).
        if not first_output_recorded:
            first_output_recorded = True
            _first_output_seconds.labels(source="token").observe(
                time.monotonic() - ttft_started
            )
        await bridge.publish(run_id, "token", frame)
```

`ttft_started` 在 `:437` 才赋值，而 `_publish_token` 定义在 `:379` —— 闭包在**调用时**读取，赋值发生在任何 token 帧之前，所以没问题。但 `ttft_started` 目前是普通局部变量，要提到 `_publish_token` 定义之前初始化，或者声明为 `nonlocal`/提前 `ttft_started = time.monotonic()`。实施时把 `:437` 的赋值提到 `:379` 之前，紧跟 `event_seq` 初始化。

- [ ] **Step 5: node 路径打点**

`sse.py:470` 那段：

```python
                        if not first_chunk_seen:
                            ttft = time.monotonic() - ttft_started
                            _session_first_node_seconds.observe(ttft)
                            if getattr(record, "is_resume", False):
                                _durable_resume_seconds.observe(ttft)
                            first_chunk_seen = True
                        jsonable_chunk = _to_jsonable(chunk)
                        # 一期 Task 3 —— agent 节点的 updates 帧是无 token 流
                        # 时用户第一次看到内容的时刻。必须挑 agent 那帧:
                        # first_chunk_seen 认的是任意第一个节点,有 recall 的
                        # run 那是召回完成,比真实首字早好几秒。
                        if not first_output_recorded and isinstance(jsonable_chunk, dict):
                            if "agent" in jsonable_chunk:
                                first_output_recorded = True
                                _first_output_seconds.labels(source="node").observe(
                                    time.monotonic() - ttft_started
                                )
```

`first_output_recorded` 在闭包外定义，这里要能写 —— 把它放在 `run_agent` 函数体的局部作用域（`_publish_token` 用 `nonlocal` 访问），这段直接赋值即可。

注意顺序：`jsonable_chunk = _to_jsonable(chunk)` 要在判断之前，所以把判断插在它之后。

- [ ] **Step 6: 跑测试确认通过**

```bash
uv run pytest services/orchestrator/tests/test_first_output_metric.py -v
```
Expected: PASS

- [ ] **Step 7: 全量校验 + Commit**

```bash
uv run pytest -v -m "not integration" -k "sse or run_agent or first_output"
uv run ruff check
uv run mypy services/orchestrator/src
git add services/orchestrator
git commit -m "feat(observability): first_output_seconds 双 source + 老 ttft 指标改名"
```

---

## Task 4: bench 脚本 + 跑第一次基线

**Files:**
- Create: `tools/bench/conftest.py`
- Create: `tools/bench/entry_latency.py`
- Create: `tools/bench/README.md`
- Create: `tools/bench/baselines/` （产出一个 YAML）
- Test: `tools/bench/test_entry_latency.py`

**`tools/` 不是包** —— 没有 `__init__.py`，`from tools.bench.x import y` 会 `ModuleNotFoundError`。仓库约定见 `tools/eval/conftest.py`：目录自己塞进 `sys.path`，测试用裸模块名 import。本 task 照抄。`pyproject.toml:186` 的 `testpaths` 已含 `tools/*`，pytest 能收集到。

**Interfaces:**
- Consumes: Task 1 的 8 个 span 名、Task 2 的 `group` 字段。
- Produces: `tools/bench/baselines/<date>-before.yaml` —— Task 5 的 PR 描述引用它做对照。

- [ ] **Step 1: 写 conftest shim + 纯函数的测试（先失败）**

新建 `tools/bench/conftest.py`（照 `tools/eval/conftest.py` 逐字）：

```python
"""Put this directory on ``sys.path`` so the tests can ``import entry_latency``.

``tools/bench`` is a dev tool, not an installed workspace package — the
script lives next to its tests. Same shape as ``tools/eval/conftest.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
```

脚本的取数和网络部分不测，**分段聚合的纯函数要测**。新建 `tools/bench/test_entry_latency.py`：

```python
"""分段聚合的纯函数测试。网络/真栈部分不在单测范围。"""
from entry_latency import Segment, aggregate  # noqa: E402


def test_aggregate_reports_median_and_p95_per_segment() -> None:
    runs = [
        {"记忆召回": 100.0, "规划": 200.0},
        {"记忆召回": 300.0, "规划": 200.0},
        {"记忆召回": 200.0, "规划": 200.0},
    ]
    out = aggregate(runs)
    assert out["记忆召回"].median == 200.0
    assert out["规划"].median == 200.0


def test_aggregate_tolerates_a_segment_missing_from_some_runs() -> None:
    """rerank 只在配了 reranker 时才有 span;缺席的轮次不能拉低中位数,
    要按"出现过的轮次"算,并记录出现次数。"""
    runs = [{"记忆重排": 100.0}, {}, {"记忆重排": 300.0}]
    out = aggregate(runs)
    assert out["记忆重排"].median == 200.0
    assert out["记忆重排"].n == 2
```

第二个测试针对一个真实陷阱：把缺席当 0 会让「配了 reranker 的 agent」和「没配的」混在一起时中位数完全失真。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tools/bench/test_entry_latency.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'entry_latency'`

- [ ] **Step 3: 写脚本**

`tools/bench/entry_latency.py`：

```python
"""入口链延迟取数脚本 —— 一期 Task 4。

跑 N 轮固定 prompt,每轮从 trace facade 拉 span 树,按 ``group == "entry"``
的 span 加首个 llm_call 聚合出各段耗时,输出 median / p95。

不是 benchmark 框架,是个取数脚本。二期量 P1.1/P1.2/P1.3/P3 复用它。

``tools/bench`` 不是包(见 conftest.py),所以按脚本路径跑,不是 ``-m``::

    uv run python tools/bench/entry_latency.py \\
        --agent my-agent@1.0.0 --prompt-file tools/bench/prompts/fixed.txt --runs 10 \\
        --out tools/bench/baselines/2026-07-27-before.yaml
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    """One entry-chain stage's aggregated latency across runs."""

    median: float
    p95: float
    n: int


def aggregate(runs: list[dict[str, float]]) -> dict[str, Segment]:
    """Fold per-run segment timings into median / p95 per segment.

    A segment absent from a run is **skipped**, not counted as zero — an
    optional stage (rerank only exists when a reranker is configured) would
    otherwise drag its own median toward zero and make two differently
    configured agents incomparable. ``Segment.n`` records how many runs
    actually had the stage.
    """
    names = {name for run in runs for name in run}
    out: dict[str, Segment] = {}
    for name in names:
        values = sorted(run[name] for run in runs if name in run)
        if not values:
            continue
        out[name] = Segment(
            median=statistics.median(values),
            p95=values[min(len(values) - 1, int(len(values) * 0.95))],
            n=len(values),
        )
    return out
```

取数 + CLI 部分（`main()`、httpx 拉 trace facade、YAML 写盘）在同一文件实现。基线 YAML 照 `tools/eval/baselines/` 的形状带 `meta.fingerprints`：

```yaml
segments:
  记忆召回: {median: 2010.0, p95: 2400.0, n: 10}
  规划: {median: 1600.0, p95: 1900.0, n: 10}
first_output:
  median: 4200.0
  p95: 5100.0
meta:
  commit: 0b3238d2
  host: darwin-arm64 / local dev compose
  agent: my-agent@1.0.0
  runs: 10
  note: before 连接池改造
```

`host` / `commit` 必填 —— 换机器数字不可比，标记让人一眼看出而不是默默误比。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tools/bench/test_entry_latency.py -v
```
Expected: PASS

- [ ] **Step 5: 起真栈，跑第一次基线**

参照仓库既有的 live smoke 配方起本地全栈（compose + Keycloak + 一个配了 memory 的测试 agent）。然后：

```bash
uv run python tools/bench/entry_latency.py \
    --agent <测试 agent>@<ver> --prompt-file tools/bench/prompts/fixed.txt --runs 10 \
    --out tools/bench/baselines/$(date +%F)-before.yaml
```

**同时观察 span 自身开销**（spec §7 的风险项）：对比开 span 前后的 `first_output` median。若 span 让首字慢了超过 50ms，说明粒度过细，回头砍掉 `resolve_mode` 这类毫秒级的段。

- [ ] **Step 6: Commit**

```bash
git add tools/bench
git commit -m "feat(bench): 入口链延迟取数脚本 + 改造前基线"
```

---

## Task 5: 连接池复用（8 处）

**Files:**
- Modify: `services/orchestrator/src/orchestrator/llm/providers/openai.py:241-350`
- Modify: `services/orchestrator/src/orchestrator/llm/providers/anthropic.py:~320-395`
- Modify: `services/orchestrator/src/orchestrator/llm/embedder.py:75-90`
- Modify: `services/orchestrator/src/orchestrator/llm/rerank.py:~55-70`
- Modify: `services/orchestrator/src/orchestrator/tools/web_search.py:~110-130`
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py:205-265`
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py:491-560, 2163, 2206`
- Modify: `services/orchestrator/src/orchestrator/llm/providers/openai_compatible.py`（7 个工厂透传）
- Modify: `services/control-plane/src/control_plane/runtime.py`、`app.py`
- Test: `services/orchestrator/tests/test_http_client_reuse.py`

**Interfaces:**
- Consumes: 无（独立于前四个 task 的产物）。
- **必须在 Task 4 跑完基线之后 cut**；且与 Task 1 同改 `runtime.py`，不与 Task 1 并行。

- [ ] **Step 1: 写测试（先失败）**

新建 `services/orchestrator/tests/test_http_client_reuse.py`：

```python
"""共享 httpx 客户端的注入与回退 —— 一期 Task 5。"""


async def test_reuses_the_injected_client_across_calls() -> None:
    """注入 http 时,两次调用必须复用同一个 client 实例(不新建、不关闭)。"""
    shared = httpx.AsyncClient(transport=_stub_transport())
    client = HTTPOpenAIClient(api_key="k", http=shared)
    await client.chat_completions(model="m", messages=[], tools=None)
    await client.chat_completions(model="m", messages=[], tools=None)
    assert not shared.is_closed


async def test_does_not_close_the_injected_client() -> None:
    """命门:原代码是 ``async with``,退出即关。注入的是进程级共享 client,
    被某一次调用关掉的话后续所有 LLM 调用全炸,而且是运行时才炸。"""
    shared = httpx.AsyncClient(transport=_stub_transport())
    client = HTTPOpenAIClient(api_key="k", http=shared)
    await client.chat_completions(model="m", messages=[], tools=None)
    assert not shared.is_closed


async def test_falls_back_to_per_call_client_when_not_injected() -> None:
    """http=None(测试/eval CLI/未接线路径)行为与改造前一致。"""
    client = HTTPOpenAIClient(api_key="k", transport=_stub_transport())
    result = await client.chat_completions(model="m", messages=[], tools=None)
    assert result is not None


async def test_streaming_path_keeps_the_no_read_timeout() -> None:
    """流式那条原本是 ``httpx.Timeout(self.timeout_s, read=None)``;走共享
    client 后 timeout 必须 per-request 传,否则 idle_timeout_s 的语义被
    client 级 timeout 抢走,长思考的模型会被误杀。"""
    seen: list[httpx.Timeout | float | None] = []

    class _RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout"))
            return httpx.Response(200, text='data: [DONE]\n\n')

    shared = httpx.AsyncClient(transport=_RecordingTransport())
    client = HTTPOpenAIClient(api_key="k", http=shared, timeout_s=30.0)
    async for _ in client.stream_chat_completions(model="m", messages=[], tools=None):
        pass

    # httpx 把 Timeout 摊平成 extensions["timeout"] 的 dict,read=None 表示
    # 不设读超时 —— 这正是流式路径依赖的语义。
    assert seen and seen[0]["read"] is None
```

`stream_chat_completions` 的真实方法名以 `openai.py` 为准（`:339` 附近那个流式方法），实施时对齐。`_stub_transport()` 是本文件内的小 helper，返回一个固定 200 JSON 的 `MockTransport`。

第二和第四个测试是这个 task 的命门。第二个防「共享 client 被关」这个只在运行时暴露的错误；第四个防 timeout 语义漂移。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest services/orchestrator/tests/test_http_client_reuse.py -v
```
Expected: FAIL — `TypeError: unexpected keyword argument 'http'`

- [ ] **Step 3: 改造 `HTTPOpenAIClient`**

`openai.py:241`，dataclass 加字段：

```python
    #: 一期 Task 5 —— 进程级共享客户端。``None`` 时退回 per-call 建(测试 /
    #: eval CLI / 未接线路径,行为与改造前逐字节一致)。注入时**不得**关闭它:
    #: 它属于 control-plane 的 lifespan,不属于任何一次调用。
    http: httpx.AsyncClient | None = None
```

加一个上下文管理器把两条路径统一：

```python
@asynccontextmanager
async def _client_for(
    shared: httpx.AsyncClient | None,
    *,
    timeout: float | httpx.Timeout,
    transport: httpx.AsyncBaseTransport | None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield the shared client (never closing it) or a per-call one (closed on
    exit). The shared branch must not close: the client outlives every call."""
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        yield client
```

`chat_completions`（`:289`）改成：

```python
        try:
            async with _client_for(
                self.http, timeout=self.timeout_s, transport=self.transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}{self.chat_completions_path}",
                    headers={...},
                    json=body,
                    timeout=self.timeout_s,   # per-request，共享 client 时生效
                )
```

流式那条（`:341-343`）同理，per-request 传 `httpx.Timeout(self.timeout_s, read=None)`。

- [ ] **Step 4: 同样改造另外五个 client 类**

`HTTPAnthropicClient`（`anthropic.py:328/389`）、`HTTPEmbeddingClient`（`embedder.py:75`）、rerank 的 client（`rerank.py:63`）、`TavilyClient`（`web_search.py:120`）、`SupervisorClient`（`sandbox.py:259`）—— 每个都加 `http` 字段 + 走 `_client_for`。

`_client_for` 放在一个共享位置（建议 `orchestrator/llm/providers/_http.py` 新建，或 common 的 http 工具模块），六处 import 同一个，**不要各抄一份** —— 六份实现里只要有一份忘了「共享分支不关闭」，就是运行时炸。

`sandbox.py:214` 那个工厂形式（`return httpx.AsyncClient(...)` 而非 `async with`）跟其余五处形状不同，单独看它的生命周期语义再改。`web_search` / `sandbox` 各带自己的 transport 与 timeout 语义，逐个确认没漂。

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest services/orchestrator/tests/test_http_client_reuse.py -v
```
Expected: PASS

- [ ] **Step 6: 接线 —— lifespan 建 client**

`services/control-plane/src/control_plane/app.py` 的 lifespan（参照 `:2379` 已有的 `httpx.AsyncClient` 先例）：

```python
    # 一期 Task 5 —— 进程级共享 HTTP 客户端。httpx 自己按 (scheme, host, port)
    # 分连接池,所以一个实例就够,不需要按 provider 分。keepalive 让每次 LLM /
    # embed / rerank 调用省掉一次 TLS 握手。
    shared_http = httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=64, max_connections=256),
    )
    app.state.shared_http = shared_http
    try:
        yield
    finally:
        await shared_http.aclose()
```

- [ ] **Step 7: 接线 —— 串到各构造点**

- `build_agent`（`agent_factory.py:491`）加 kwarg `http_client: httpx.AsyncClient | None = None`，传到 `:2163` 的 `HTTPAnthropicClient(...)`、`:2206` 的 `HTTPOpenAIClient(...)`，以及 `openai_compatible.py` 那 7 个 `make_*_client` 工厂（工厂签名加同名参数透传）。注意 `agent_factory` 的 dict（`:2214`）只映射 5 个，`self_hosted` / `azure` 走别的分支，**两处都要接**。
- `ResolvingEmbedder` / `ResolvingReranker`（`runtime.py:810/860`）构造时传入，`HTTPEmbeddingClient(api_key=api_key, http=self.http)`。
- ToolEnv 的 `web_search_client` / `supervisor_client`（`assembly.py:115/45`）在 control-plane 构造时传入。

- [ ] **Step 8: 跑第二次 bench，写对照**

```bash
uv run python tools/bench/entry_latency.py \
    --agent <同一个 agent> --prompt-file tools/bench/prompts/fixed.txt --runs 10 \
    --out tools/bench/baselines/$(date +%F)-after.yaml
```

**同一台机器、同一个 agent、同一个 prompt**，否则数字不可比。把 before/after 对照表写进 PR 描述。

若测出提升很小，如实写 —— spec §非目标已经写明不承诺幅度，「握手不是瓶颈」本身是二期该知道的结论。

- [ ] **Step 9: 全量校验 + Commit**

```bash
uv run pytest -v -m "not integration" --timeout=120 --timeout-method=thread
uv run pytest -v -m integration    # 需 DOCKER_HOST
uv run ruff check
uv run mypy packages services/orchestrator/src
git add services tools
git commit -m "perf(http): 8 处 HTTP 调用复用进程级连接池"
```

---

## Task 6: TraceView 分解条 + 配色分组

**Files:**
- Modify: `apps/admin-ui/src/api/trace_facade.ts:27-46`
- Create: `apps/admin-ui/src/pages/agent_detail/playground/entry_breakdown.ts`
- Create: `apps/admin-ui/src/pages/agent_detail/playground/EntryBreakdown.tsx`
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/TraceView.tsx:99-140, 224-235`
- Modify: `apps/admin-ui/src/i18n/locales/{zh-CN,en}.ts`
- Test: `apps/admin-ui/src/pages/agent_detail/playground/__tests__/entry_breakdown.test.ts`

**Interfaces:**
- Consumes: Task 2 的 `TraceSpan.group`。

- [ ] **Step 1: 写分段算法测试（先失败）**

新建 `__tests__/entry_breakdown.test.ts`：

```typescript
import { describe, expect, it } from "vitest";
import { buildBreakdown } from "../entry_breakdown";
import type { TraceSpan } from "../../../../api/trace_facade";

const span = (o: Partial<TraceSpan>): TraceSpan => ({
  id: "x", parentId: null, kind: "span", label: "l", detail: null,
  startMs: 0, latencyMs: 0, model: null, inputTokens: null, outputTokens: null,
  costUsd: null, input: null, output: null, level: "default",
  statusMessage: null, purpose: "", group: null, ...o,
});

describe("buildBreakdown", () => {
  it("takes only top-level entry spans, not their children", () => {
    // 命门:recall 的子 span 也带 group="entry",全算进去的话总和会
    // 超过 recall 本身,分解条宽度加起来大于 100%。
    const spans = [
      span({ id: "r", label: "记忆召回", group: "entry", startMs: 0, latencyMs: 2000 }),
      span({ id: "e", parentId: "r", label: "向量化", group: "entry", startMs: 10, latencyMs: 200 }),
    ];
    const out = buildBreakdown(spans);
    expect(out.map((s) => s.label)).toEqual(["记忆召回"]);
  });

  it("ends the bar at the first llm span", () => {
    const spans = [
      span({ id: "r", label: "记忆召回", group: "entry", startMs: 0, latencyMs: 2000 }),
      span({ id: "l", kind: "llm", label: "LLM 调用", startMs: 2000, latencyMs: 600 }),
      span({ id: "l2", kind: "llm", label: "LLM 调用", startMs: 5000, latencyMs: 600 }),
    ];
    const out = buildBreakdown(spans);
    expect(out.at(-1)?.label).toBe("LLM 调用");
    expect(out).toHaveLength(2);
  });

  it("returns an empty breakdown when the trace has no entry spans", () => {
    expect(buildBreakdown([span({ kind: "llm", label: "LLM 调用" })])).toEqual([]);
  });
});
```

第一个测试是命门：`group === "entry"` 会同时命中父子，只取顶层（`parentId` 不在本组内的）才对。

- [ ] **Step 2: 跑测试确认失败**

```bash
node "$(dirname "$(which pnpm)")/../lib/node_modules/corepack/dist/pnpm.js" -C apps/admin-ui vitest run src/pages/agent_detail/playground/__tests__/entry_breakdown.test.ts
```
Expected: FAIL — 模块不存在

- [ ] **Step 3: 前端类型加 group**

`api/trace_facade.ts:27` 的 `TraceSpan` 加：

```typescript
  /** Functional stage for colour grouping + the pre-first-token breakdown:
   *  "entry" = entry chain (recall / ingest / context gates), null = other.
   *  Separate from `purpose`, which is LLM-call intent only. */
  group: "entry" | null;
```

- [ ] **Step 4: 写分段算法**

`entry_breakdown.ts`：

```typescript
/**
 * Pre-first-token breakdown — 一期 Task 6。
 *
 * Folds a run's trace into the segments shown above the waterfall: each
 * top-level entry-chain span, then the first LLM call. Pure — no fetching.
 */
import type { TraceSpan } from "../../../api/trace_facade";

export interface BreakdownSegment {
  id: string;
  label: string;
  latencyMs: number;
}

export function buildBreakdown(spans: readonly TraceSpan[]): BreakdownSegment[] {
  const entry = spans.filter((s) => s.group === "entry");
  if (entry.length === 0) return [];
  // Children of an entry span also carry group="entry" (recall's embed /
  // retrieve / …). Counting them would make the segments sum past the
  // parent's own latency and the bar wider than the elapsed time — take
  // only the ones whose parent is outside this group.
  const entryIds = new Set(entry.map((s) => s.id));
  const topLevel = entry.filter((s) => s.parentId === null || !entryIds.has(s.parentId));

  const firstLlm = spans
    .filter((s) => s.kind === "llm")
    .sort((a, b) => a.startMs - b.startMs)[0];

  const segments = topLevel
    .slice()
    .sort((a, b) => a.startMs - b.startMs)
    .map((s) => ({ id: s.id, label: s.label, latencyMs: s.latencyMs }));

  if (firstLlm !== undefined) {
    segments.push({ id: firstLlm.id, label: firstLlm.label, latencyMs: firstLlm.latencyMs });
  }
  return segments;
}
```

- [ ] **Step 5: 跑测试确认通过**

```bash
node "$(dirname "$(which pnpm)")/../lib/node_modules/corepack/dist/pnpm.js" -C apps/admin-ui vitest run src/pages/agent_detail/playground/__tests__/entry_breakdown.test.ts
```
Expected: PASS

- [ ] **Step 6: 写组件**

`EntryBreakdown.tsx`：

```tsx
/**
 * Pre-first-token breakdown bar — 一期 Task 6。
 *
 * Sits above the waterfall and answers the one question the waterfall makes
 * you hunt for: where did the time before the first token go? Clicking a
 * segment selects the matching span in the tree below (shared `selectedId`).
 */
import { Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { TraceSpan } from "../../../api/trace_facade";
import { buildBreakdown } from "./entry_breakdown";
import { fmtDuration } from "./duration_format";

/** Below this share of the bar a segment shows colour only — its label would
 *  overflow into its neighbours. */
const LABEL_MIN_SHARE = 0.06;

interface EntryBreakdownProps {
  spans: readonly TraceSpan[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function EntryBreakdown({ spans, selectedId, onSelect }: EntryBreakdownProps) {
  const { t } = useTranslation();
  const segments = buildBreakdown(spans);
  if (segments.length === 0) return null;

  const total = segments.reduce((sum, s) => sum + s.latencyMs, 0);
  if (total <= 0) return null;

  return (
    <div style={{ marginBottom: 12 }} data-testid="entry-breakdown">
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {t("trace.breakdown_title", { d: fmtDuration(total) })}
      </Typography.Text>
      <div style={{ display: "flex", gap: 2, marginTop: 4, height: 22 }}>
        {segments.map((s) => {
          const share = s.latencyMs / total;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => onSelect(s.id)}
              title={`${s.label} · ${fmtDuration(s.latencyMs)}`}
              style={{
                flex: `${share} 1 0`,
                minWidth: 4,
                border: selectedId === s.id ? "1px solid var(--ew-text-primary)" : "none",
                borderRadius: 3,
                background: "var(--ew-trace-entry, #7c8cff)",
                color: "#fff",
                fontSize: 11,
                overflow: "hidden",
                whiteSpace: "nowrap",
                cursor: "pointer",
                padding: 0,
              }}
            >
              {share >= LABEL_MIN_SHARE ? `${s.label} ${fmtDuration(s.latencyMs)}` : ""}
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

`<button>` 而非 `<div onClick>` —— Playwright + axe 那条 CI 会抓不可聚焦的交互元素。

i18n 加键前**先 grep 是否撞既有**（同一 object 内重复键 esbuild 静默覆盖）：

```bash
grep -n "breakdown" apps/admin-ui/src/i18n/locales/zh-CN.ts
```

两个 locale 各加：`trace.breakdown_title` —— zh-CN `"首字 {{d}}"`，en `"First output {{d}}"`。注意 `zh-CN.ts:1257` 已有一个 `ttft: "首字 {{d}}"`（TurnCard 用），**不要复用它** —— 那个在 `turn` 命名空间下，语义是「这一轮的首字」，这里是「trace 的首字分解」，两者会各自演化。

- [ ] **Step 7: 挂进 TraceView + 配色**

`TraceView.tsx:99` 附近渲染 `<EntryBreakdown spans={spans} selectedId={selectedId} onSelect={setSelectedId} />`（复用已有的 `selectedId` 状态，点击即高亮，无需新状态）。

`kindDotColor` / `kindBarColor`（`:224-235`）在 `kind === "tool"` 之后加一档：

```typescript
  if (span.group === "entry") return ENTRY;
```

`ENTRY` 用一个跟 llm 蓝 / tool 紫都区分得开的色值，且要过双主题（照文件里既有色值常量的定义方式加）。

- [ ] **Step 8: 全量校验**

```bash
node "$(dirname "$(which pnpm)")/../lib/node_modules/corepack/dist/pnpm.js" -C apps/admin-ui tsc -b --noEmit
node "$(dirname "$(which pnpm)")/../lib/node_modules/corepack/dist/pnpm.js" -C apps/admin-ui vitest run
```
Expected: tsc 干净；vitest 全绿（含既有 TraceView 测试不回归）。

**编辑器报的错不算数** —— 本仓库诊断大面积 stale，只认这两条命令的输出。

- [ ] **Step 9: Commit**

```bash
git add apps/admin-ui
git commit -m "feat(admin-ui): TraceView 首字分解条 + 入口链 span 配色分组"
```

---

## 收尾

六个 task 全绿后：

1. 手动开一次调试台，跑一轮有 memory 的 agent，确认 TraceView 顶部分解条的数字跟 `tools/bench/baselines/<date>-after.yaml` 对得上（spec §6.2 的第三行）。
2. 按 spec §6.3 逐条核对成功判据。
3. 用 superpowers:finishing-a-development-branch 收尾。
