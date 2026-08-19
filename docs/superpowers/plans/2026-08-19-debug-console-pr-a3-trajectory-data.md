# 调试台 PR-A.3「轨迹补数据」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 PR-A.2 的轨迹视图补齐三处缺的数据与 UI(SYSTEM 行 / Schema tab / 模型块 TTFT 双色),并修掉真栈冒烟逮到的两条 Langfuse 计时 bug。

**Architecture:** 后端三处小改(orchestrator 多发一帧 `system_prompt`、`AIMessage.additional_kwargs.first_token_ms`、`BuiltAgent.tool_catalog` + 控制面新端点 `GET /v1/agents/{name}/{version}/tools`)+ 控制面两处修正(`trace_facade` 时长用 end−start、spans 按 startMs 排序)+ 对外平面过滤 `system_prompt` 帧;前端在既有账本数据层加一种 `system` 行与 `firstTokenMs`,详情多 `Schema` tab,时间轴块按比例双色。**§九 的形态不动。**

**Tech Stack:** Python 3.12 / FastAPI / LangGraph(orchestrator + control-plane,同进程);React 19 + TypeScript + antd + vitest + Playwright(admin-ui)。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` **§十**(形态与裁定都在那里;§九 是它的前提)。

## Global Constraints

- **it 数只增不减**:每个 task 新增测试 ≥ 1 条、删 0 条、不改既有断言(既有断言若因新帧 / 新字段而变,先证明它变得对,再改)。测试先红后绿,写时即绿的断言要做一次变异自证。
- **e2e 是行为清单**:改任何带 `data-testid` 的组件前 `grep -rn <testid> apps/admin-ui/e2e`;不删既有 testid。
- **i18n 三处同步**:`apps/admin-ui/src/i18n/locales/en.ts` 的接口块 + en 值块 + `zh-CN.ts`,新键**追加在 `console` 块末尾**;zh-CN 标点用半角(与 PR-A.2 终审 M8 一致)。
- **文件 ≤ 400 行**(admin-ui `src/components/console/` 下新文件同样守);`RowDetailPayloadResult.tsx` 现在正好 400 行 —— 新面板一律新文件。
- **颜色只走 `--ew-*` 令牌**;两主题都要能分。
- **对外平面零新暴露**:`system_prompt` 帧对第三方 API key 的实时流与回放都不可见;对外文档站(`apps/admin-ui/docs-site/`)不改。
- **不动 list**:中栏对话视图 / 过程条 / Gantt / `parseTimeline` 的既有分支;状态栏「首 token」芯片;对外 SSE 文档。
- **Python 门**:改动包内 `uv run pytest <files> -q`(仓库根运行;orchestrator 测试前缀 `DOCKER_HOST=` 清空)+ `uv run ruff check` + `uv run ruff format --check` + `uv run mypy services/orchestrator/src packages`(CI 同款范围,不含 control-plane);control-plane 只跑 pytest + ruff。
- **前端门**:`pnpm typecheck`(`tsc -b`,裸 `tsc --noEmit` 恒绿不算)/ `pnpm exec vitest run <受影响目录>`(波末全量)/ `pnpm build` / `pnpm build-storybook` / `pnpm exec playwright test e2e/playground-upload.spec.ts e2e/session-history.spec.ts`。
- **同一谓词只写一处**:「上下文行」(user / system)的判断只在 `ledger_collapse.CONTEXT_KINDS`;Rule 2 的排序与后端排序各自独立成立(前端防御,不依赖后端)。
- **子代理纪律**(沿用):测试前台跑;不 `git checkout --` / stash / reset 还原实验(复制到 scratchpad 再改回);`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock` 只在 integration 测试需要时设。

---

## 文件结构

**后端(orchestrator)**
- Modify `services/orchestrator/src/orchestrator/tools/registry.py` — `ToolCatalogEntry` + `ToolRegistry.catalog()`。
- Modify `services/orchestrator/src/orchestrator/agent_factory.py` — `BuiltAgent.tool_catalog`。
- Modify `services/orchestrator/src/orchestrator/sse.py` — `_system_prompt_of(graph_input)` + `system_prompt` 帧;`sse_consumer(hide_events=...)`。
- Modify `services/orchestrator/src/orchestrator/graph_builder/streaming_redact.py` — `TokenSink.first_delta_at`。
- Modify `services/orchestrator/src/orchestrator/graph_builder/builder.py` — `agent_node` 写 `first_token_ms`。

**后端(control-plane)**
- Modify `services/control-plane/src/control_plane/api/agents.py` — `GET /{name}/{version}/tools`;`run_agent_for_user` 传 `hide_events`。
- Modify `services/control-plane/src/control_plane/api/mcp_servers.py` — `/{name}/tools` 补 `input_schema`。
- Modify `services/control-plane/src/control_plane/api/trace_facade.py` — 时长 end−start、spans 排序。
- Modify `services/control-plane/src/control_plane/api/runs.py` — `spawn_run(hide_events=...)`。
- Modify `services/control-plane/src/control_plane/api/_run_event_stream.py` — `build_event_producer(hide_events=...)`。
- Modify `services/control-plane/src/control_plane/api/external_events.py` / `external_approvals.py` — 传 `EXTERNAL_HIDDEN_EVENTS`。
- Modify `docs/api/streaming-events.md` — 事件表加 `system_prompt`(控制台平面)。

**前端(admin-ui,`src/` 相对)**
- Modify `api/timeline.ts` — `AgentStep.firstTokenMs`。
- Modify `api/trajectory_rows.ts` — `SystemRow`、`AssistantRow.firstTokenMs`、`ledgerRowsOf` 前置 SYSTEM 行。
- Modify `api/trace_match.ts` — Rule 2 按 `startMs` 排序。
- Modify `api/agents.ts` — `getAgentTools` + 类型。
- Modify `components/console/ledger_types.ts` — `LedgerRecord.firstTokenAt`、`LedgerRequest.firstTokenMs`。
- Modify `components/console/ledger.ts` — `system` 泳道 / 内容 / 折叠相同提示词 / `firstTokenAt`。
- Modify `components/console/ledger_timeline.ts` — `TimelineSpan.ttft`。
- Modify `components/console/ledger_collapse.ts` — `CONTEXT_KINDS` + 三处引用。
- Modify `components/console/TrajectoryLedger.tsx` — `foldContextOf` 引 `CONTEXT_KINDS`。
- Create `components/console/useAgentTools.ts` — 懒加载工具 schema 的 hook。
- Create `components/console/RowDetailSchema.tsx` — Schema 面板。
- Create `components/console/RowDetailSystem.tsx` — SYSTEM 原文面板。
- Modify `components/console/RecordDetails.tsx` — system tabs、schema tab。
- Modify `components/console/RowDetailPayloadResult.tsx` — `export` `JsonBlock`(一行)。
- Modify `components/console/TrajectoryView.tsx` — `agentName` / `agentVersion` prop + hook。
- Modify `pages/agent_detail/PlaygroundTab.tsx` — 传 `r.name` / `r.version`。
- Modify `components/console/TrajectoryTimelineBlocks.tsx` / `trajectory_timeline.css` / `trajectory_timeline_pointer.ts` — 双色块 + 提示行。
- Modify `components/console/RequestDetails.tsx` — 「首 token」行。
- Modify `i18n/locales/en.ts` / `zh-CN.ts` — 新键(见各 task)。

## 波次 / 并行(SDD worktree)

| 波 | 任务 | 文件冲突 |
|---|---|---|
| 1 | T1 tool_catalog · T2 system_prompt 帧 · T3 first_token_ms · T4 trace_facade 修正 · T5 前端数据行(timeline/trajectory_rows)· T6 trace_match 排序 | 两两无共同文件 |
| 2 | T7 tools 端点 · T8 对外平面过滤 · T9 账本层 · T10 Schema tab UI | T7 / T8 都碰 `agents.py`(不同区域,波末合并解冲突);T9 / T10 都追加 i18n(同上) |
| 3 | T11 TTFT 双色 · T12 SYSTEM 行 UI | 只有 i18n 追加 |
| 4 | T13 发布 + 真栈冒烟(合并后) | — |

---

### Task 1: `BuiltAgent.tool_catalog`(orchestrator)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/registry.py:76-120`(`ToolSpec` 之后)、`:386-425`(读方法区)
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py:196-262`(`BuiltAgent`)、`:1105-1133`(`return BuiltAgent(...)`)
- Test: `services/orchestrator/tests/test_tool_registry_catalog.py`(新)

**Interfaces:**
- Produces: `ToolCatalogEntry(name: str, description: str, parameters: Mapping[str, Any], source: str, from_skill: str | None, deferred: bool)`(frozen dataclass,`orchestrator.tools.registry`);`ToolRegistry.catalog() -> tuple[ToolCatalogEntry, ...]`(注册顺序,含 deferred);`BuiltAgent.tool_catalog: tuple[ToolCatalogEntry, ...] = ()`。T7 的端点读 `built.tool_catalog`。

- [ ] **Step 1: 写失败测试**

```python
# services/orchestrator/tests/test_tool_registry_catalog.py
"""PR-A.3 — ``ToolRegistry.catalog()``:控制面 Schema tab 要的「整个注册表」投影。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestrator.tools.registry import ToolCatalogEntry, ToolContext, ToolRegistry, ToolResult, ToolSpec


class _T:
    def __init__(self, name: str, *, from_skill: str | None = None) -> None:
        self._spec = ToolSpec(
            name=name,
            description=f"desc {name}",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            from_skill=from_skill,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="")


def test_catalog_lists_every_tool_in_registration_order_with_source_and_deferred() -> None:
    reg = ToolRegistry()
    reg.register(_T("bash"))
    reg.register(_T("mcp__gh__create_issue"), source="mcp:gh", deferred=True)
    reg.register(_T("skill_tool", from_skill="writer"))

    cat = reg.catalog()

    assert [c.name for c in cat] == ["bash", "mcp__gh__create_issue", "skill_tool"]
    assert cat[0] == ToolCatalogEntry(
        name="bash",
        description="desc bash",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        source="builtin",
        from_skill=None,
        deferred=False,
    )
    assert cat[1].source == "mcp:gh" and cat[1].deferred is True
    assert cat[2].from_skill == "writer"
    # specs() 不含 deferred,catalog() 含 —— 两者的差就是 deferred 集合。
    assert {c.name for c in cat if c.deferred} == {c.name for c in cat} - {s.name for s in reg.specs()}
```

> `ToolRegistry.register(tool, *, deferred=False, source=None)`(`registry.py:366`)、`ToolContext` / `ToolResult` 都在 `orchestrator.tools.registry`(`:155` / `:251`)—— 已核对。

- [ ] **Step 2: 跑,确认红**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_tool_registry_catalog.py -q`
Expected: FAIL — `ImportError: cannot import name 'ToolCatalogEntry'`

- [ ] **Step 3: 实现**

`registry.py`,紧挨 `ToolSpec` 之后:

```python
@dataclass(frozen=True)
class ToolCatalogEntry:
    """One row of the registry's "everything registered" projection (PR-A.3).

    The console's Schema tab wants the JSON Schema the model was handed
    (``parameters``) plus provenance — including tools that are deferred
    (not in ``specs()``) so a promoted-on-demand call still resolves.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    #: ``source_of(name)`` — ``"builtin"`` / ``"mcp:<server>"`` / ...
    source: str
    from_skill: str | None
    deferred: bool
```

`ToolRegistry`,紧挨 `all_specs` 之后:

```python
    def catalog(self) -> tuple[ToolCatalogEntry, ...]:
        """Every registered tool — active and deferred — in registration order."""
        return tuple(
            ToolCatalogEntry(
                name=name,
                description=tool.spec.description,
                parameters=dict(tool.spec.parameters),
                source=self.source_of(name),
                from_skill=tool.spec.from_skill,
                deferred=name in self._deferred,
            )
            for name, tool in self._tools.items()
        )
```

`agent_factory.py` `BuiltAgent` 末尾加字段(带注释,照 `token_budget` 的写法):

```python
    #: PR-A.3 — the build's full tool registry projection
    #: (``ToolRegistry.catalog()``) for the console's Schema tab. Read-only
    #: metadata; nothing on the run path consumes it.
    tool_catalog: tuple[ToolCatalogEntry, ...] = ()
```

`return BuiltAgent(...)` 里加 `tool_catalog=registry.catalog(),`(放在 `tool_replay_safe=` 旁)。`from orchestrator.tools.registry import ToolCatalogEntry` 补进已有的 registry 导入行。

- [ ] **Step 4: 跑,确认绿 + 既有测试不变**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_tool_registry_catalog.py services/orchestrator/tests/test_agent_factory.py -q && uv run mypy services/orchestrator/src && uv run ruff check services/orchestrator && uv run ruff format --check services/orchestrator`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/registry.py services/orchestrator/src/orchestrator/agent_factory.py services/orchestrator/tests/test_tool_registry_catalog.py
git commit -m "feat(orchestrator): ToolRegistry.catalog() + BuiltAgent.tool_catalog —— 控制面 Schema tab 的数据源"
```

---

### Task 2: `system_prompt` 帧 + `sse_consumer(hide_events)`(orchestrator)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/sse.py:282-302`(`run_agent` 签名不动)、`:514-522`(`metadata` 帧与 `ttft_started` 之间)、`:1421-1429`(`sse_consumer` 签名)+ 它的 yield 循环
- Modify: `docs/api/streaming-events.md:14-20`(事件表)
- Test: `services/orchestrator/tests/test_sse.py`(追加)、`services/orchestrator/tests/test_sse_persistence.py`(追加)

**Interfaces:**
- Produces: SSE 帧 `event: system_prompt`,`data = {"text": str}`,落库;`sse_consumer(..., hide_events: frozenset[str] = frozenset())`;模块常量 `SYSTEM_PROMPT_EVENT = "system_prompt"`(`orchestrator.sse`)。T8 传 `hide_events`。

- [ ] **Step 1: 写失败测试(三条)**

在 `test_sse.py` 末尾追加(复用文件里已有的 `_ScriptedGraph` / `_new_record` / `_drain`;`SystemMessage` / `HumanMessage` 从 `langchain_core.messages` 导入):

```python
@pytest.mark.asyncio
async def test_run_agent_emits_system_prompt_frame_right_after_metadata() -> None:
    """PR-A.3 §十.1 —— graph_input 首条是 SystemMessage 时,metadata 之后紧跟一帧
    system_prompt(落库、占 seq),再才是 updates。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge, run_manager=rm, record=record, graph=graph,
        graph_input={"messages": [SystemMessage(content="你是评审员"), HumanMessage(content="hi")]},
        config={},
    )

    events = await _drain(bridge, record.run_id)
    assert [e.event for e in events] == ["metadata", "system_prompt", "updates"]
    assert events[1].data == {"text": "你是评审员"}
    seqs = [int(e.id.split("-")[1]) for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_run_agent_skips_system_prompt_frame_without_system_message() -> None:
    """resume(graph_input=None)/ 审批续跑(Command)/ 没有 SystemMessage 首条 —— 不发。"""
    for graph_input in (None, {"messages": [HumanMessage(content="hi")]}, {"messages": []}):
        bridge = InMemoryStreamBridge()
        rm = RunManager()
        record = await _new_record(rm)
        await run_agent(
            bridge=bridge, run_manager=rm, record=record,
            graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
            graph_input=graph_input, config={},
        )
        events = await _drain(bridge, record.run_id)
        assert "system_prompt" not in [e.event for e in events], graph_input


@pytest.mark.asyncio
async def test_sse_consumer_hide_events_filters_frames_but_keeps_ids_monotonic() -> None:
    """对外平面用 hide_events 滤掉 system_prompt;其余帧原样(id 不重排)。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge, run_manager=rm, record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": [SystemMessage(content="secret"), HumanMessage(content="hi")]},
        config={},
    )

    async def never_disconnected() -> bool:
        return False

    wire = b"".join([
        chunk async for chunk in sse_consumer(
            bridge=bridge, record=record, run_manager=rm,
            is_disconnected=never_disconnected, hide_events=frozenset({"system_prompt"}),
        )
    ])
    assert b"event: system_prompt" not in wire
    assert b"secret" not in wire
    assert b"event: metadata" in wire and b"event: updates" in wire and b"event: end" in wire
```

在 `test_sse_persistence.py` 末尾追加(照该文件里 `test_run_agent_mirrors_metadata_and_updates_to_event_store` 的写法拿 `event_store` 与 `list`):

```python
@pytest.mark.asyncio
async def test_system_prompt_frame_is_persisted_with_its_seq() -> None:
    """帧落库 —— 回放(控制台历史轮)才看得到 SYSTEM 行。"""
    # 复制上面那条已有测试的搭建(bridge / rm / record / InMemoryRunEventStore),
    # graph_input 换成 {"messages": [SystemMessage(content="sp"), HumanMessage(content="hi")]},
    # 然后:
    rows = await event_store.list(run_id=record.run_id, since_seq=None, limit=50)
    names = [r.event_name for r in rows]
    assert names[:2] == ["metadata", "system_prompt"]
    assert rows[1].data == {"text": "sp"}
```

- [ ] **Step 2: 跑,确认红**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_sse.py services/orchestrator/tests/test_sse_persistence.py -q -k "system_prompt or hide_events"`
Expected: 4 条 FAIL(帧不存在 / `hide_events` 未知参数)。

- [ ] **Step 3: 实现**

`sse.py` 模块级(靠近 `DEFAULT_STREAM_MODE`):

```python
#: PR-A.3 §十.1 — the run's final system prompt, one frame right after
#: ``metadata``. Console-plane only: external producers pass it in
#: ``hide_events`` (see ``sse_consumer``).
SYSTEM_PROMPT_EVENT = "system_prompt"


def _system_prompt_of(graph_input: Any) -> str | None:
    """The ``SystemMessage`` text at the head of a fresh run's input, else None.

    Resume (``graph_input=None``) and approval continuation (``Command``) carry
    no fresh prompt — nothing to report (the earlier run's frame is in the store).
    """
    if not isinstance(graph_input, Mapping):
        return None
    messages = graph_input.get("messages")
    if not isinstance(messages, Sequence) or not messages:
        return None
    first = messages[0]
    if getattr(first, "type", None) != "system":
        return None
    content = getattr(first, "content", None)
    return content if isinstance(content, str) and content else None
```

`run_agent` 里 `await _publish_frame("metadata", metadata_payload)` 之后、`ttft_started = time.monotonic()` **之前**插:

```python
        # PR-A.3 §十.1 — the prompt the model actually got, before the TTFT
        # clock starts (server-synthesised, not LLM output).
        system_prompt = _system_prompt_of(graph_input)
        if system_prompt is not None:
            await _publish_frame(SYSTEM_PROMPT_EVENT, {"text": system_prompt})
```

`sse_consumer` 加参数 `hide_events: frozenset[str] = frozenset()`,在把 `StreamEvent` 翻成 SSE 帧的循环里,**在 `is_end` 判断之后、`format_sse` 之前**加 `if entry.event in hide_events: continue`(心跳哨兵与 end 帧不受影响;过滤只发生在输出侧,不碰 bridge 的 seq)。docstring 补一句「对外平面用它滤掉 console-only 帧;被滤帧的 id 照常被跳过,客户端按 seq 续传不受影响」。

`docs/api/streaming-events.md` 事件表加一行:`| \`system_prompt\` | Once, right after \`metadata\`, when the run starts fresh (console plane only — external producers filter it) | yes |`。

- [ ] **Step 4: 跑,确认绿 + 全部 sse 测试不退**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_sse.py services/orchestrator/tests/test_sse_persistence.py services/orchestrator/tests/test_sse_plan_events.py -q && uv run mypy services/orchestrator/src && uv run ruff check services/orchestrator docs && uv run ruff format --check services/orchestrator`
Expected: 全绿(既有精确帧序断言用的 `graph_input={"messages": []}` 不带 SystemMessage,不受影响;若有别的测试因新帧而红,先判它断的是「精确帧序」还是「某帧存在」,前者加 `"system_prompt"`,后者不该红)。

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/src/orchestrator/sse.py services/orchestrator/tests/test_sse.py services/orchestrator/tests/test_sse_persistence.py docs/api/streaming-events.md
git commit -m "feat(orchestrator): run 开头多发一帧 system_prompt + sse_consumer(hide_events) 给对外平面过滤"
```

---

### Task 3: 每步首 token 时刻 `first_token_ms`(orchestrator)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/streaming_redact.py:175-199`(`TokenSink.__init__` / `__call__`)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:899-928`(`agent_node` 的 LLM 调用块)
- Test: `services/orchestrator/tests/test_streaming_redact.py`(追加)、`services/orchestrator/tests/test_first_token_ms.py`(新)

**Interfaces:**
- Produces: `TokenSink.first_delta_at: float | None`(`time.monotonic()`);`AIMessage.additional_kwargs["first_token_ms"]: int`(只在有 sink 且收到过非空 delta 时写)。前端 T5 读 `additional_kwargs.first_token_ms`。

- [ ] **Step 1: 写失败测试**

`test_streaming_redact.py` 末尾追加(文件里应已有 TokenSink 的搭建;`LLMDelta` 从 `orchestrator.llm.providers._streaming` 导入):

```python
@pytest.mark.asyncio
async def test_token_sink_records_first_non_empty_delta_time_once() -> None:
    published: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        published.append(frame)

    sink = TokenSink(step=1, publish=publish, dlp=False, screen=False)
    assert sink.first_delta_at is None
    await sink(LLMDelta())  # 空 delta(只有 role 之类)不算首 token
    assert sink.first_delta_at is None
    await sink(LLMDelta(reasoning="thinking"))
    first = sink.first_delta_at
    assert first is not None
    await sink(LLMDelta(content="answer"))
    assert sink.first_delta_at == first  # 只记第一次
```

`test_first_token_ms.py`(新文件;搭建整段照抄 `test_token_step_alignment.py` 的 `_StreamingLLM` / `_run_and_collect`,只改两点:caller 在第一个 delta 前 `await asyncio.sleep(0.02)`;收集的是 `updates` 里 AIMessage 的 `additional_kwargs`):

```python
"""PR-A.3 §十.3 — agent_node 把「LLM 调用起点 → 第一个非空 delta」写进
AIMessage.additional_kwargs["first_token_ms"];没流式就不写。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from orchestrator.graph_builder import build_react_graph
from orchestrator.graph_builder._config import TOKEN_SINK_KEY
from orchestrator.graph_runner import GraphRunner, make_checkpointer
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.tools.registry import ToolRegistry


@dataclass
class _SlowFirstTokenLLM:
    first_token_delay_s: float
    streams: bool = True
    calls: int = field(default=0)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[object],
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        if self.streams and on_delta is not None:
            await asyncio.sleep(self.first_token_delay_s)
            await on_delta(LLMDelta(content="answer"))
        return AIMessage(content="answer")


async def _first_ai_additional_kwargs(llm: Any, *, with_sink: bool) -> dict[str, Any]:
    async def capture(frame: dict[str, Any]) -> None:
        del frame

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry())
        )
        configurable: dict[str, Any] = {"thread_id": str(uuid4())}
        if with_sink:
            configurable[TOKEN_SINK_KEY] = capture
        cfg: RunnableConfig = {"configurable": configurable}
        async for chunk in compiled.astream(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 3},
            config=cfg,
            stream_mode="updates",
        ):
            for _node, ch in chunk.items():
                if not isinstance(ch, dict):
                    continue
                for m in ch.get("messages", []) or []:
                    if isinstance(m, AIMessage):
                        return dict(m.additional_kwargs)
    raise AssertionError("no AIMessage in updates")


@pytest.mark.asyncio
async def test_first_token_ms_written_when_streaming() -> None:
    ak = await _first_ai_additional_kwargs(_SlowFirstTokenLLM(0.03), with_sink=True)
    assert isinstance(ak.get("first_token_ms"), int)
    assert ak["first_token_ms"] >= 25  # 30ms 睡眠,允许计时抖动


@pytest.mark.asyncio
async def test_first_token_ms_absent_without_sink_or_without_stream() -> None:
    no_sink = await _first_ai_additional_kwargs(_SlowFirstTokenLLM(0.0), with_sink=False)
    assert "first_token_ms" not in no_sink
    no_stream = await _first_ai_additional_kwargs(_SlowFirstTokenLLM(0.0, streams=False), with_sink=True)
    assert "first_token_ms" not in no_stream
```

> `build_react_graph` / `GraphRunner` / `make_checkpointer` 的导入路径以 `test_token_step_alignment.py` 顶部为准(照抄)。

- [ ] **Step 2: 跑,确认红**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_streaming_redact.py services/orchestrator/tests/test_first_token_ms.py -q -k "first_token or first_non_empty"`
Expected: FAIL(`first_delta_at` 属性不存在 / `first_token_ms` 缺)。

- [ ] **Step 3: 实现**

`streaming_redact.py` `TokenSink.__init__` 末尾:`self.first_delta_at: float | None = None`(文档:「第一个非空 delta 的 `time.monotonic()`;agent_node 拿它算 `first_token_ms`」)。`__call__` 开头:

```python
        if self.first_delta_at is None and (delta.content or delta.reasoning or delta.tool_calls):
            self.first_delta_at = time.monotonic()
```
(`import time` 补上。)

`builder.py` `agent_node`,`else:` 分支(`_token_sink = make_token_sink(...)` 之前)加 `llm_started = time.monotonic()`;`if _token_sink is not None: await _token_sink.flush()` 之后加:

```python
            # PR-A.3 §十.3 — per-step TTFT, same channel as the tool path's
            # ``ToolMessage.additional_kwargs["duration_ms"]``: rides the
            # ``updates`` frame into the store, so history replays keep it.
            if _token_sink is not None and _token_sink.first_delta_at is not None:
                response.additional_kwargs["first_token_ms"] = round(
                    (_token_sink.first_delta_at - llm_started) * 1000
                )
```
(`time` 已在 builder.py 导入则不重复。)

- [ ] **Step 4: 跑,确认绿 + 邻近测试不退**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_streaming_redact.py services/orchestrator/tests/test_first_token_ms.py services/orchestrator/tests/test_token_step_alignment.py services/orchestrator/tests/test_graph_builder.py -q && uv run mypy services/orchestrator/src && uv run ruff check services/orchestrator && uv run ruff format --check services/orchestrator`
Expected: 全绿(`test_graph_builder.py` 若不存在,换 `ls services/orchestrator/tests | grep builder` 找到的那个)。

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/src/orchestrator/graph_builder/streaming_redact.py services/orchestrator/src/orchestrator/graph_builder/builder.py services/orchestrator/tests/test_streaming_redact.py services/orchestrator/tests/test_first_token_ms.py
git commit -m "feat(orchestrator): 每步首 token 时刻写进 AIMessage.additional_kwargs.first_token_ms"
```

---

### Task 4: Langfuse 计时两处修正(control-plane `trace_facade`)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/trace_facade.py:104-185`(trace 级)、`:285-310`(`_parse_observation`)
- Test: `services/control-plane/tests/test_trace_facade_normalize.py`(追加)

**Interfaces:**
- Produces: `latencyMs` 在有 `start_time`/`end_time` 时 = 差值毫秒;`spans` 按 `(startMs, id)` 稳定升序。前端 T6 独立防御。

- [ ] **Step 1: 写失败测试**

`test_trace_facade_normalize.py` 末尾追加(`_obs` / `_trace` 是文件顶部已有的 helper;被测函数 `normalize_trace(trace)`(`trace_facade.py:100`);`from datetime import UTC, datetime, timedelta`):

```python
def test_latency_prefers_end_minus_start_over_langfuse_latency_field() -> None:
    """真栈 2026-08-19:测试集群这版 Langfuse 的 ``latency`` 是毫秒,旧代码当秒 ×1000
    → 计时 tab「121m5s」。有 start/end 就用差值;``latency`` 只兜底。"""
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    obs = [
        _obs("root", "SPAN", "expert_work.session.run", None, 46268.0, 0,
             end_time=t0 + timedelta(milliseconds=46268)),
        _obs("llm", "GENERATION", "expert_work.orchestrator.llm_call", "root", 29786.0, 1,
             end_time=t0 + timedelta(seconds=1, milliseconds=29786)),
    ]
    out = normalize_trace(_trace(obs))
    by_id = {s["id"]: s for s in out["spans"]}
    assert by_id["root"]["latencyMs"] == 46268
    assert by_id["llm"]["latencyMs"] == 29786
    assert out["trace"]["latencyMs"] == 46268 + 1000  # max(end) - min(start)


def test_latency_falls_back_to_langfuse_field_when_end_time_missing() -> None:
    obs = [_obs("root", "SPAN", "expert_work.session.run", None, 1.5, 0)]  # 无 end_time 属性
    out = normalize_trace(_trace(obs))
    assert out["spans"][0]["latencyMs"] == 1500


def test_spans_are_sorted_by_start_ms_stably() -> None:
    """Langfuse 按创建倒序回 observations;前端 Rule 2 按数组序配对,必须先排。"""
    obs = [
        _obs("root", "SPAN", "expert_work.session.run", None, 40.0, 0),
        _obs("llm2", "GENERATION", "expert_work.orchestrator.llm_call", "root", 7.0, 33),
        _obs("tool", "SPAN", "expert_work.tool.call", "root", 1.5, 30),
        _obs("llm1", "GENERATION", "expert_work.orchestrator.llm_call", "root", 29.0, 1),
    ]
    out = normalize_trace(_trace(obs))
    starts = [s["startMs"] for s in out["spans"]]
    assert starts == sorted(starts)
    llm_ids = [s["id"] for s in out["spans"] if s["kind"] == "llm"]
    assert llm_ids == ["llm1", "llm2"]
```

> `_obs` 里 `GENERATION` 是否映射成 `kind == "llm"`,`expert_work.tool.call` 是否映射成 tool —— 照文件里既有测试用的名字改,不要猜。

- [ ] **Step 2: 跑,确认红**

Run: `uv run pytest services/control-plane/tests/test_trace_facade_normalize.py -q -k "end_minus_start or sorted_by_start or falls_back"`
Expected: 前两条 FAIL(`latencyMs` 为 ×1000 值 / 顺序未排);第三条可能已绿(兜底路径未变)—— 它是回归护栏,保留。

- [ ] **Step 3: 实现**

`_parse_observation`:

```python
    end_time = getattr(o, "end_time", None)
    if o.start_time is not None and end_time is not None:
        latency_ms = max(0, round((end_time - o.start_time).total_seconds() * 1000))
    else:
        latency_ms = round((o.latency or 0) * 1000)
```

trace 级(`raw_observations` 非空分支,算完 `trace_start` 后):

```python
    end_times = [
        getattr(o, "end_time", None) for o in raw_observations if getattr(o, "end_time", None) is not None
    ]
    if trace_start is not None and end_times:
        trace_latency_ms = max(0, round((max(end_times) - trace_start).total_seconds() * 1000))
```
(空 observations 分支保持原 `round((t.latency or 0) * 1000)`。)

spans 列表:`for parsed in sorted(parsed_by_id.values(), key=lambda p: (p.start_ms, p.id))`。

- [ ] **Step 4: 跑,确认绿**

Run: `uv run pytest services/control-plane/tests/test_trace_facade_normalize.py services/control-plane/tests/test_trace_facade_endpoint.py -q && uv run ruff check services/control-plane && uv run ruff format --check services/control-plane`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add services/control-plane/src/control_plane/api/trace_facade.py services/control-plane/tests/test_trace_facade_normalize.py
git commit -m "fix(control-plane): Langfuse 计时用 end-start 而非 latency×1000;spans 按 startMs 稳定排序"
```

---

### Task 5: 前端数据行 —— `SystemRow` + `firstTokenMs`(`api/timeline.ts`、`api/trajectory_rows.ts`)

**Files:**
- Modify: `apps/admin-ui/src/api/timeline.ts:13-40`(`AgentStep`)、`:211-226`(push agent)
- Modify: `apps/admin-ui/src/api/trajectory_rows.ts:50-67`(类型)、`:152-163`(assistant 行)、`:283-289`(`ledgerRowsOf`)
- Test: `apps/admin-ui/src/api/__tests__/timeline.test.ts`、`apps/admin-ui/src/api/__tests__/trajectory_rows.test.ts`(都追加;路径以 `ls apps/admin-ui/src/api/__tests__ | grep -E "timeline|trajectory_rows"` 为准)

**Interfaces:**
- Produces:
  - `AgentStep.firstTokenMs?: number`(`additional_kwargs.first_token_ms`,没报 `undefined`)。
  - `export type SystemRow = RowBase & { kind: "system"; text: string }`;`TrajectoryRow = UserRow | SystemRow | CompactRow | AssistantRow`(**不进** `CompactRow`,中栏投影看不到它)。
  - `AssistantRow.firstTokenMs?: number`。
  - `ledgerRowsOf(events, input)` 返回 `[SystemRow?, UserRow, ...]`:events 里第一帧 `event === "system_prompt"` 且 `data.text` 非空 → 首条 `{ id: "system", kind: "system", seq: -1, step: null, status: "ok", durationMs: null, eventIndexes: [i], serverMs: serverMsOf(events[i].id), text }`。
- T9 / T12 消费 `SystemRow`;T9 / T11 消费 `firstTokenMs`。

- [ ] **Step 1: 写失败测试**

`timeline.test.ts` 追加:

```ts
it("reads additional_kwargs.first_token_ms into AgentStep.firstTokenMs (undefined when absent)", () => {
  const withTtft = parseTimeline([
    evt("updates", { agent: { step_count: 1, messages: [{ type: "ai", content: "a", additional_kwargs: { first_token_ms: 812 }, usage_metadata: {} }] } }, "1000-1"),
  ]);
  expect(withTtft[0]).toMatchObject({ kind: "agent", firstTokenMs: 812 });
  const without = parseTimeline([
    evt("updates", { agent: { step_count: 1, messages: [{ type: "ai", content: "a", usage_metadata: {} }] } }, "1000-1"),
  ]);
  expect((without[0] as AgentStep).firstTokenMs).toBeUndefined();
});
```
(`evt(...)` 用该测试文件已有的帧构造 helper;没有就照文件里最近一条测试的写法手写一个 `SseEvent`。)

`trajectory_rows.test.ts` 追加:

```ts
describe("ledgerRowsOf — SYSTEM row (PR-A.3 §十.1)", () => {
  const input = { text: "hi", attachmentNames: [], inputs: {} };
  it("prepends a system row when the run carries a system_prompt frame", () => {
    const events: SseEvent[] = [
      evt("metadata", { run_id: "r", thread_id: "t" }, "1000-1"),
      evt("system_prompt", { text: "你是评审员\n第二行" }, "1001-2"),
      evt("updates", { agent: { step_count: 1, messages: [{ type: "ai", content: "ok", usage_metadata: {} }] } }, "1500-3"),
    ];
    const rows = ledgerRowsOf(events, input);
    expect(rows.map((r) => r.kind)).toEqual(["system", "user", "assistant"]);
    expect(rows[0]).toMatchObject({ id: "system", kind: "system", text: "你是评审员\n第二行", seq: -1, eventIndexes: [1], serverMs: 1001 });
  });
  it("no frame / empty text → no system row; compact projection never sees it", () => {
    const events: SseEvent[] = [evt("system_prompt", { text: "" }, "1001-2")];
    expect(ledgerRowsOf(events, input).map((r) => r.kind)).toEqual(["user"]);
    expect(ledgerRowsOf([], input).map((r) => r.kind)).toEqual(["user"]);
    expect(compactRowsOf([evt("system_prompt", { text: "x" }, "1001-2")]).map((r) => r.kind)).not.toContain("system");
  });
  it("assistant row carries firstTokenMs from the step", () => {
    const events: SseEvent[] = [
      evt("updates", { agent: { step_count: 1, messages: [{ type: "ai", content: "ok", additional_kwargs: { first_token_ms: 640 }, usage_metadata: {} }] } }, "1500-3"),
    ];
    const assistant = ledgerRowsOf(events, input).find((r) => r.kind === "assistant");
    expect(assistant).toMatchObject({ kind: "assistant", firstTokenMs: 640 });
  });
});
```

- [ ] **Step 2: 跑,确认红**

Run: `cd apps/admin-ui && pnpm exec vitest run src/api/__tests__/timeline.test.ts src/api/__tests__/trajectory_rows.test.ts`
Expected: 新增 4 条 FAIL(字段 undefined / kind 序不对)。

- [ ] **Step 3: 实现**

`timeline.ts`:`AgentStep` 加 `/** additional_kwargs.first_token_ms —— LLM 调用起点到第一个非空 delta 的毫秒;后端没写(无流式 / judge 开着)就是 undefined。 */ firstTokenMs?: number;`;push 里加 `firstTokenMs: optInt(ak.first_token_ms),`。

`trajectory_rows.ts`:
```ts
export type SystemRow = RowBase & { kind: "system"; text: string };
export type AssistantRow = RowBase & { kind: "assistant"; /* ...既有... */ firstTokenMs?: number };
export type TrajectoryRow = UserRow | SystemRow | CompactRow | AssistantRow;
```
assistant 行构造加 `firstTokenMs: item.firstTokenMs,`。新增:

```ts
/** PR-A.3 §十.1 —— run 开头那帧 `system_prompt` → 账本里轮首的 SYSTEM 行。
 *  只认第一帧;`data.text` 空就当没有。中栏投影(`compactRowsOf`)永远不出它。 */
function systemRowOf(events: readonly SseEvent[]): SystemRow | null {
  const i = events.findIndex((e) => e.event === "system_prompt");
  if (i === -1) return null;
  const data = events[i].data;
  const text = data !== null && typeof data === "object" && typeof (data as { text?: unknown }).text === "string"
    ? (data as { text: string }).text
    : "";
  if (text === "") return null;
  return {
    id: "system", kind: "system", seq: -1, step: null, status: "ok", durationMs: null,
    eventIndexes: [i], serverMs: serverMsOf(events[i].id), text,
  };
}

export function ledgerRowsOf(events: readonly SseEvent[], input: TrajectoryInput): TrajectoryRow[] {
  const system = systemRowOf(events);
  return [...(system === null ? [] : [system]), userRowOf(input), ...rowsOf(events, { projection: "ledger" })];
}
```
(`serverMsOf` 从 `./sse_id` 导入。)`ledgerRowsOf` 的 docstring 加一句 SYSTEM 行。

- [ ] **Step 4: 跑,确认绿 + typecheck 看清楚谁要跟上**

Run: `cd apps/admin-ui && pnpm exec vitest run src/api && pnpm typecheck`
Expected: vitest 绿;`pnpm typecheck` **预计红**在 `components/console/ledger.ts`(`LANE_OF_KIND` 穷尽表 / `contentOf` switch)、`kind_label.ts` 等处 —— 那是 T9 的工作,**本 task 不改 components/**。把红的文件清单写进报告。若 `api/` 自己有穷尽 switch 红了(`resolveGanttKey` 等),在本 task 修。

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/timeline.ts apps/admin-ui/src/api/trajectory_rows.ts apps/admin-ui/src/api/__tests__/timeline.test.ts apps/admin-ui/src/api/__tests__/trajectory_rows.test.ts
git commit -m "feat(console): 轨迹数据层 —— system_prompt 帧 → SystemRow,first_token_ms → firstTokenMs"
```

---

### Task 6: `trace_match` Rule 2 按 `startMs` 配对(前端防御)

**Files:**
- Modify: `apps/admin-ui/src/api/trace_match.ts:90-101`
- Test: `apps/admin-ui/src/api/__tests__/trace_match.test.ts`(追加)

- [ ] **Step 1: 写失败测试**

```ts
it("Rule 2 pairs step rows with main llm spans in startMs order, not payload order (PR-A.3 §十.4)", () => {
  const rows = [assistantRow({ id: "assistant:1", seq: 1, step: 1 }), assistantRow({ id: "assistant:3", seq: 3, step: 2 })];
  const spans = [
    span({ id: "llm-late", kind: "llm", purpose: "main", startMs: 32903, latencyMs: 7265 }),
    span({ id: "llm-early", kind: "llm", purpose: "main", startMs: 960, latencyMs: 29786 }),
  ];
  const result = matchTraceSpans(rows, traceOf(spans));
  expect(result.get("assistant:1")?.span?.id).toBe("llm-early");
  expect(result.get("assistant:3")?.span?.id).toBe("llm-late");
});
```
(`assistantRow` / `span` / `traceOf` 用该测试文件已有的 fixture helper;没有就照文件内既有 Rule 2 测试的写法造。)

- [ ] **Step 2: 跑,确认红**

Run: `cd apps/admin-ui && pnpm exec vitest run src/api/__tests__/trace_match.test.ts`
Expected: 1 FAIL(配反)。

- [ ] **Step 3: 实现**

```ts
  const mainLlmSpans = spans
    .filter((span) => span.kind === "llm" && (span.purpose === "" || span.purpose === "main"))
    // 后端现在按 startMs 排了(PR-A.3),但配对的正确性不该依赖上游顺序。
    .slice()
    .sort((a, b) => a.startMs - b.startMs);
```

- [ ] **Step 4: 跑,确认绿**

Run: `cd apps/admin-ui && pnpm exec vitest run src/api/__tests__/trace_match.test.ts && pnpm typecheck`

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/trace_match.ts apps/admin-ui/src/api/__tests__/trace_match.test.ts
git commit -m "fix(console): trace_match Rule 2 按 startMs 配对,不再信 Langfuse 返回顺序"
```

---

### Task 7: `GET /v1/agents/{name}/{version}/tools` + MCP 租户侧补 `input_schema`(control-plane)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py:1585`(紧挨 `list_revisions` 之前插新端点;复用 `_CONSOLE_ONLY` / `_get_runtime` / `ensure_single_tenant_scope` / `ensure_resource_access` / `emit`)
- Modify: `services/control-plane/src/control_plane/api/mcp_servers.py:965`
- Test: `services/control-plane/tests/test_agents_tools_endpoint.py`(新)、`services/control-plane/tests/test_mcp_servers_api.py:899-917`(追加断言)、`services/control-plane/tests/test_external_route_reachability.py`(跑,不改)

**Interfaces:**
- Consumes: `BuiltAgent.tool_catalog`(T1)。
- Produces: `GET /v1/agents/{name}/{version}/tools?tenant_id=` → `{"success": true, "data": {"items": [{"name","description","parameters","source","from_skill","deferred"}], "total": N}}`;404 未知 agent;422 构建失败(`agent manifest cannot be built: …`,与 `run_agent` 同)。T10 的 `getAgentTools` 消费。

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_agents_tools_endpoint.py
"""PR-A.3 §十.2 — GET /v1/agents/{name}/{version}/tools:整个工具注册表(含 deferred)。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.runtime.runs import InMemoryRunStore, RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import AgentFactoryError
from orchestrator.agent_factory import BuiltAgent
from orchestrator.tools.registry import ToolCatalogEntry
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt
from tests.test_agents_api import _VALID_YAML  # 同目录既有的合法 manifest

_CATALOG = (
    ToolCatalogEntry(name="bash", description="run a shell command",
                     parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                     source="builtin", from_skill=None, deferred=False),
    ToolCatalogEntry(name="mcp__gh__create_issue", description="create an issue", parameters={"type": "object"},
                     source="mcp:gh", from_skill=None, deferred=True),
)


def _runtime(*, fail: bool = False) -> AgentRuntime:
    async def _build(spec: object, *, tenant_id: object | None = None, user_id: str | None = None) -> BuiltAgent:
        del spec, tenant_id, user_id
        if fail:
            raise AgentFactoryError("no model key")
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1, tool_catalog=_CATALOG)  # type: ignore[arg-type]

    return AgentRuntime(run_manager=RunManager(store=InMemoryRunStore()), stream_bridge=InMemoryStreamBridge(), agent_builder=_build)


@pytest.fixture
async def client_factory():
    async def make(runtime: AgentRuntime) -> AsyncClient:
        settings = Settings(env="dev", auth_mode="dev", rate_limit_burst=10_000, rate_limit_per_second=10_000.0,
                            oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE])
        app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier(), agent_runtime=runtime)
        headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://control-plane.test", headers=headers)
    return make


@pytest.mark.asyncio
async def test_tools_endpoint_returns_full_catalog(client_factory) -> None:
    async with await client_factory(_runtime()) as client:
        assert (await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})).status_code == 201
        r = await client.get("/v1/agents/code-reviewer/1.0.0/tools")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["total"] == 2
        assert data["items"][0] == {
            "name": "bash", "description": "run a shell command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            "source": "builtin", "from_skill": None, "deferred": False,
        }
        assert data["items"][1]["deferred"] is True and data["items"][1]["source"] == "mcp:gh"


@pytest.mark.asyncio
async def test_tools_endpoint_404_unknown_and_422_when_build_fails(client_factory) -> None:
    async with await client_factory(_runtime()) as client:
        assert (await client.get("/v1/agents/nope/1.0.0/tools")).status_code == 404
    async with await client_factory(_runtime(fail=True)) as client:
        assert (await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})).status_code == 201
        r = await client.get("/v1/agents/code-reviewer/1.0.0/tools")
        assert r.status_code == 422
        assert "cannot be built" in r.text


@pytest.mark.asyncio
async def test_tools_endpoint_is_console_only(client_factory) -> None:
    """API key(对外平面)不能读工具 schema —— 它是管理面产物。照 test_agents_api
    里既有的 console_only 测试取 API-key 头的写法(grep 'console_only' 那几条)。"""
    ...
```

> 第三条的 API-key 请求头怎么造,照 `tests/test_agents_api.py` 里带 `console_only` 字样的既有测试抄(它们已经有 mint key 的 helper);断言状态码与那些测试一致(403 或 404,以既有测试为准)。`_VALID_YAML` 的 agent 名 / 版本以文件内为准。

`test_mcp_servers_api.py::test_server_tools_lists_live_tools` 末尾加:`assert "input_schema" in r.json()["data"][0]`(看 `_fake_probe_ok` 给的 `MCPToolDef.input_schema` 是什么,精确断言它)。

- [ ] **Step 2: 跑,确认红**

Run: `uv run pytest services/control-plane/tests/test_agents_tools_endpoint.py services/control-plane/tests/test_mcp_servers_api.py -q -k "tools"`
Expected: 新端点 404(路由不存在)/ mcp 断言 KeyError。

- [ ] **Step 3: 实现**

`agents.py`,`list_revisions` 之前:

```python
    class AgentToolItem(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str
        description: str
        parameters: dict[str, Any]
        source: str
        from_skill: str | None
        deferred: bool

    class AgentToolList(BaseModel):
        model_config = ConfigDict(extra="forbid")
        items: list[AgentToolItem]
        total: int

    @router.get("/{name}/{version}/tools", dependencies=_CONSOLE_ONLY)
    async def list_agent_tools(
        name: str,
        version: str,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """PR-A.3 §十.2 — the agent's full tool registry (JSON Schema included).

        Builds the agent exactly like a run would (``runtime.get_agent`` is
        LRU-cached, so after the first run this is a dict read) and projects
        ``BuiltAgent.tool_catalog``. Read-only; no run is started.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal, tenant_id, audit, trace_id=current_trace_id_hex(),
            endpoint="GET /v1/agents/{name}/{version}/tools",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            record = await repo.get(tenant_id=target_tenant, name=name, version=version)
        if record is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await ensure_resource_access(request, resource="manifest", action="read", attrs=_record_attrs(record))
        try:
            built = await runtime.get_agent(
                tenant_id=target_tenant, name=name, version=version, spec=record.spec,
                user_id=request.state.principal.subject_id,
            )
        except AgentFactoryError as exc:
            raise HTTPException(status_code=422, detail=f"agent manifest cannot be built: {exc}") from exc
        await emit(
            audit, tenant_id=target_tenant, actor_id=request.state.actor_id,
            action=AuditAction.MANIFEST_READ, resource_type="manifest",
            resource_id=f"{name}/{version}/tools", trace_id=current_trace_id_hex(),
        )
        items = [
            AgentToolItem(name=t.name, description=t.description, parameters=dict(t.parameters),
                          source=t.source, from_skill=t.from_skill, deferred=t.deferred)
            for t in built.tool_catalog
        ]
        payload = AgentToolList(items=items, total=len(items))
        return JSONResponse({"success": True, "data": payload.model_dump(mode="json")})
```
(`BaseModel` 放到模块级与 `RevisionSummary` 同区更合习惯 —— 看文件里 `RevisionSummary` 定义在哪就放哪。`request.state.principal.subject_id` 的取法照 `agents.py:1232`。)

`mcp_servers.py:965`:`"data": [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in tools]`(与 `mcp_catalog.py:301-306` 一致;若 `MCPToolDef.input_schema` 可能为 None,照 catalog 那边的处理)。

- [ ] **Step 4: 跑,确认绿 + 路由可达护栏**

Run: `uv run pytest services/control-plane/tests/test_agents_tools_endpoint.py services/control-plane/tests/test_agents_api.py services/control-plane/tests/test_mcp_servers_api.py services/control-plane/tests/test_external_route_reachability.py -q && uv run ruff check services/control-plane && uv run ruff format --check services/control-plane`
Expected: 全绿(`test_external_route_reachability` 若因新的 3 段路由红了,说明注册位置不对,按它的 docstring 调)。

- [ ] **Step 5: Commit**

```bash
git add services/control-plane/src/control_plane/api/agents.py services/control-plane/src/control_plane/api/mcp_servers.py services/control-plane/tests/test_agents_tools_endpoint.py services/control-plane/tests/test_mcp_servers_api.py
git commit -m "feat(control-plane): GET /v1/agents/{name}/{version}/tools 工具 JSON Schema 端点;租户侧 mcp-servers/{name}/tools 补 input_schema"
```

---

### Task 8: 对外平面过滤 `system_prompt`(control-plane)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py:112-120`(`build_event_producer` 加 `hide_events`)+ `_stream_replay` / `_stream_live` 的帧输出点
- Modify: `services/control-plane/src/control_plane/api/runs.py:897-915`(`spawn_run` 加 `hide_events`,透传给 `sse_consumer`)
- Modify: `services/control-plane/src/control_plane/api/agents.py:1337`(`run_agent_for_user` 的 `spawn_run(...)` 传 `hide_events=EXTERNAL_HIDDEN_EVENTS`)
- Modify: `services/control-plane/src/control_plane/api/external_events.py:103-110`、`external_approvals.py:311`
- Test: `services/control-plane/tests/test_external_events.py`(追加)、`services/control-plane/tests/test_agents_run_for_user.py`(追加)、`services/control-plane/tests/test_runs_api.py`(追加一条「控制台回放仍含该帧」)

**Interfaces:**
- Consumes: `sse_consumer(hide_events=...)`、`SYSTEM_PROMPT_EVENT`(T2)。
- Produces: `control_plane.api._run_event_stream.EXTERNAL_HIDDEN_EVENTS: frozenset[str] = frozenset({SYSTEM_PROMPT_EVENT})`;`build_event_producer(..., hide_events: frozenset[str] = frozenset())`;`spawn_run(..., hide_events: frozenset[str] = frozenset())`。

- [ ] **Step 1: 写失败测试**

`test_external_events.py` 追加(照文件里既有的「回放一个终态 run」测试搭建,把 run 的落库帧里塞一条 `system_prompt`;具体塞法:该文件已有往 `InMemoryRunEventStore` 预置帧的 helper,沿用):

```python
@pytest.mark.asyncio
async def test_external_replay_hides_system_prompt_frame_but_keeps_seq_cursor() -> None:
    """对外回放滤掉 system_prompt;帧 seq 不重排,next_seq / end 语义不变 —— 被滤帧是
    页里最后一帧时也要能正常收尾(游标用的是过滤前的记录)。"""
    # 预置:metadata(seq1) / system_prompt(seq2) / updates(seq3) / end
    ...
    body = r.text
    assert "event: system_prompt" not in body and "secret prompt" not in body
    assert "event: metadata" in body and "event: updates" in body and "event: end" in body
    # 只剩两帧 + end,而且第二帧的 id 还是 seq 3(不是被重编成 2)
```

`test_agents_run_for_user.py` 追加:对外 `POST /{agent_code}/runs` 的 SSE 响应正文里 `"event: system_prompt" not in body`(stub runtime 的 `build_run_graph_input` 会给 SystemMessage,所以控制台侧会发这帧;对外侧必须看不到)。

`test_runs_api.py` 追加:控制台 `POST /v1/sessions/{thread}/runs` 的 SSE 正文里 `"event: system_prompt" in body`(证明过滤只在对外平面)。

- [ ] **Step 2: 跑,确认红**

Run: `uv run pytest services/control-plane/tests/test_external_events.py services/control-plane/tests/test_agents_run_for_user.py services/control-plane/tests/test_runs_api.py -q -k "system_prompt"`
Expected: 对外两条 FAIL(帧可见)/ 控制台那条绿(T2 已发帧)。

- [ ] **Step 3: 实现**

`_run_event_stream.py`:模块级 `EXTERNAL_HIDDEN_EVENTS = frozenset({SYSTEM_PROMPT_EVENT})`(从 `orchestrator.sse` 导入常量);`build_event_producer(..., hide_events: frozenset[str] = frozenset())`;在 `_stream_replay` / `_stream_live` 里**只在把一条记录 / 实时帧编码成 SSE 字节的那一点**加 `if <event_name> in hide_events: continue` —— 分页游标(`next_seq`)、`truncated` 判定、去重集合、缺口回填全部照旧用未过滤的记录。docstring 加一段说明为什么。

`runs.py` `spawn_run(..., hide_events: frozenset[str] = frozenset())` → `sse_consumer(..., hide_events=hide_events)`;`agents.py:1337` 的调用加 `hide_events=EXTERNAL_HIDDEN_EVENTS`;`external_events.py:103` 的 `build_event_producer(...)` 加 `hide_events=EXTERNAL_HIDDEN_EVENTS`;`external_approvals.py:311` 的 `sse_consumer(...)` 加同款。

- [ ] **Step 4: 跑,确认绿**

Run: `uv run pytest services/control-plane/tests/test_external_events.py services/control-plane/tests/test_agents_run_for_user.py services/control-plane/tests/test_runs_api.py services/control-plane/tests/test_external_approvals.py -q && uv run ruff check services/control-plane && uv run ruff format --check services/control-plane`

- [ ] **Step 5: Commit**

```bash
git add services/control-plane/src/control_plane/api/_run_event_stream.py services/control-plane/src/control_plane/api/runs.py services/control-plane/src/control_plane/api/agents.py services/control-plane/src/control_plane/api/external_events.py services/control-plane/src/control_plane/api/external_approvals.py services/control-plane/tests/test_external_events.py services/control-plane/tests/test_agents_run_for_user.py services/control-plane/tests/test_runs_api.py
git commit -m "feat(control-plane): 对外平面(API key)实时流与回放过滤 system_prompt 帧"
```

---

### Task 9: 账本层 —— `system` 泳道 / 内容 / 相同提示词折叠 / `CONTEXT_KINDS` / `firstTokenAt` / `ttft`

**Files:**
- Modify: `apps/admin-ui/src/components/console/ledger_types.ts:16-50`(`LedgerRecord` 加 `firstTokenAt`)、`:52-69`(`LedgerRequest` 加 `firstTokenMs`)
- Modify: `apps/admin-ui/src/components/console/ledger.ts:40-50`(`LANE_OF_KIND` 加 `system: 0`)、`:76-112`(`contentOf` 加 `case "system"`)、`:140-200`(`turnRecordsOf` 算 `firstTokenAt`)、`:215-244`(`requestsOf` 加 `firstTokenMs`)、`:263-300`(`buildLedger` 折叠相同 SYSTEM)
- Modify: `apps/admin-ui/src/components/console/ledger_timeline.ts:17-53`(`TimelineSpanInput.firstTokenAt`、`TimelineSpan.ttft`、`spanOf`)
- Modify: `apps/admin-ui/src/components/console/ledger_collapse.ts:70-100`(`turnSummaryOf`)、`:104-112`(`collapsibleTurnKeys`)、`:167-172`(`displayRowsOf` 折叠轮保留行)
- Modify: `apps/admin-ui/src/components/console/TrajectoryLedger.tsx:57-87`(`foldContextOf`)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts` / `zh-CN.ts` — `console.traj_kind_system: "SYSTEM"`(两边同值)
- Test: `ledger.test.ts` / `ledger_timeline.test.ts` / `ledger_collapse.test.ts` / `TrajectoryLedger.test.tsx`(都追加)

**Interfaces:**
- Consumes: `SystemRow` / `AssistantRow.firstTokenMs`(T5)。
- Produces: `LedgerRecord.firstTokenAt: number | null`(绝对服务端 ms;= `startedAt + firstTokenMs` 夹到 `≤ endedAt`;非 assistant / 缺数据 null);`LedgerRequest.firstTokenMs: number | null`;`TimelineSpan.ttft: number | null`(0–1 比例,`(firstTokenAt − startedAt)/(endedAt − startedAt)`,时长 0 或缺数据 null);`export const CONTEXT_KINDS: ReadonlySet<TrajectoryRow["kind"]> = new Set(["user", "system"])`(`ledger_collapse.ts`)。T11 / T12 消费。

- [ ] **Step 1: 写失败测试**

`ledger.test.ts` 追加(用文件里已有的 turn fixture 构造方式):

```ts
describe("SYSTEM row (PR-A.3 §十.1)", () => {
  it("lane 0, content = first line, turnStart on the SYSTEM record, USER right after", () => {
    const ledger = buildLedger({ turns: [turnWithSystem("t1", "你是评审员\n只说重点")], streamTurnKey: null, nowMs: 0 });
    expect(ledger.records.slice(0, 2).map((r) => [r.kind, r.lane, r.turnStart, r.text])).toEqual([
      ["system", 0, true, "你是评审员"], ["user", 0, false, expect.any(String)],
    ]);
  });
  it("consecutive turns with the same prompt fold it; a changed prompt shows again", () => {
    const ledger = buildLedger({
      turns: [turnWithSystem("t1", "A"), turnWithSystem("t2", "A"), turnWithSystem("t3", "B"), turnWithSystem("t4", "B")],
      streamTurnKey: null, nowMs: 0,
    });
    const systems = ledger.records.filter((r) => r.kind === "system").map((r) => [r.turnKey, r.text]);
    expect(systems).toEqual([["t1", "A"], ["t3", "B"]]);
    // 折掉的轮,USER 仍是 turnStart
    expect(ledger.records.find((r) => r.turnKey === "t2")).toMatchObject({ kind: "user", turnStart: true });
  });
  it("firstTokenAt = startedAt + firstTokenMs (clamped to endedAt) and LedgerRequest.firstTokenMs", () => {
    // assistant step with first_token_ms 500 and a 2s span → firstTokenAt = start + 500
    // ...fixture with serverMs-based timing (reuse an existing timed-turn fixture in this file)
    const rec = ledger.records.find((r) => r.kind === "assistant")!;
    expect(rec.firstTokenAt).toBe(rec.startedAt! + 500);
    expect(ledger.requests[0].firstTokenMs).toBe(500);
  });
});
```
(`turnWithSystem(key, text)` 在测试文件里写一个 helper:一条 `ConsoleTurn`,events = `[metadata, system_prompt{text}, updates(agent step)]`;照文件里已有的 turn 构造 helper 改。)

`ledger_timeline.test.ts` 追加:

```ts
it("TimelineSpan.ttft is the first-token fraction in both modes; null when data missing", () => {
  const recs = [
    spanInput({ index: 0, kind: "assistant", startedAt: 1000, endedAt: 3000, firstTokenAt: 1500 }),
    spanInput({ index: 1, kind: "tool", startedAt: 3000, endedAt: 3100, firstTokenAt: null }),
    spanInput({ index: 2, kind: "assistant", startedAt: 3100, endedAt: 3100, firstTokenAt: 3100 }),
  ];
  for (const mode of ["sequence", "duration"] as const) {
    const model = deriveTimeline(recs, mode)!;
    expect(model.spans.map((s) => s.ttft)).toEqual([0.25, null, null]);
  }
});
```

`ledger_collapse.test.ts` 追加:

```ts
it("system rows are context rows: kept when the turn is collapsed, not counted as steps (CONTEXT_KINDS)", () => {
  const system = rec({ id: "s1", index: 0, turnKey: "t1", kind: "system" });
  const user = rec({ id: "u1", index: 1, turnKey: "t1", kind: "user" });
  const a1 = rec({ id: "a1", index: 2, turnKey: "t1", kind: "assistant" });
  const ledger = ledgerOf([system, user, a1], [turn({ key: "t1", lastIndex: 2 })]);
  expect(CONTEXT_KINDS.has("system") && CONTEXT_KINDS.has("user")).toBe(true);
  expect(collapsibleTurnKeys(ledger)).toEqual([]); // 只有一条非上下文记录,不值得折
  expect(turnSummaryOf([system, user, a1]).other).toBe(0);
  const rows = displayRowsOf(ledger, { collapsedTurns: new Set(["t1"]), collapsedOwners: new Set(), matches: null });
  expect(rows.map((r) => (r.kind === "record" ? r.record.id : r.kind))).toEqual(["s1", "u1", "turn-summary"]);
});
```

`TrajectoryLedger.test.tsx` 追加:双击 SYSTEM 行 → 落点同 USER(折所在轮;轮里 ≥ 2 非上下文记录时调 `onToggleTurn`)。

- [ ] **Step 2: 跑,确认红**

Run: `cd apps/admin-ui && pnpm exec vitest run src/components/console/__tests__/ledger.test.ts src/components/console/__tests__/ledger_timeline.test.ts src/components/console/__tests__/ledger_collapse.test.ts src/components/console/__tests__/TrajectoryLedger.test.tsx`
Expected: 新增条目 FAIL;`pnpm typecheck` 此时也红(T5 留下的穷尽缺口)。

- [ ] **Step 3: 实现**

- `ledger_types.ts`:`LedgerRecord` 加 `/** ASSISTANT 记录的首 token 绝对时刻(`startedAt + row.firstTokenMs`,夹到 ≤ endedAt);其它 / 缺数据 null。 */ firstTokenAt: number | null;`;`LedgerRequest` 加 `firstTokenMs: number | null;`。
- `ledger.ts`:`LANE_OF_KIND` 加 `system: 0,`;`contentOf` 加 `case "system": return { text: firstLine(row.text), resultText: null };`;`turnRecordsOf` 里算:
  ```ts
  const firstTokenAt = row.kind === "assistant" && row.firstTokenMs !== undefined && span !== null
    ? Math.min(span.start + row.firstTokenMs, span.end)
    : null;
  ```
  写进 record;`requestsOf` 加 `firstTokenMs: row.firstTokenMs ?? null,`;`buildLedger` 循环里 `let lastSystemText: string | null = null;`,非 unreplayed 分支 `const base = ledgerRowsOf(...)` 之后:
  ```ts
  // §十.1 —— 相邻轮系统提示词相同就折掉(只留第一轮与变化的轮)。
  if (base[0]?.kind === "system") {
    if (base[0].text === lastSystemText) base.shift();   // base 改成 let / 用 slice
    else lastSystemText = base[0].text;
  }
  ```
- `ledger_timeline.ts`:`TimelineSpanInput` 加 `firstTokenAt: number | null;`,`TimelineSpan` 加 `/** 首 token 在块内的比例 0–1;缺数据 / 零时长 null。 */ ttft: number | null;`,`spanOf` 算 `ttft`:
  ```ts
  function ttftOf(r: TimelineSpanInput): number | null {
    if (r.firstTokenAt === null || r.startedAt === null || r.endedAt === null) return null;
    const d = r.endedAt - r.startedAt;
    if (d <= 0) return null;
    return Math.min(1, Math.max(0, (r.firstTokenAt - r.startedAt) / d));
  }
  ```
- `ledger_collapse.ts`:`export const CONTEXT_KINDS: ReadonlySet<LedgerRecord["kind"]> = new Set(["user", "system"]);` 并把 `r.kind !== "user"` / `r.kind === "user"` 三处改成 `CONTEXT_KINDS.has(r.kind)`。
- `TrajectoryLedger.tsx` `foldContextOf`:`if (!CONTEXT_KINDS.has(record.kind))` 计数(import 自 `./ledger_collapse`)。
- i18n:`console.traj_kind_system: "SYSTEM"` 三处。
- 其余 `pnpm typecheck` 指出的穷尽点(`ledger_search.ts` 等)一并补。`kind_label.ts` 不用改(通用 `traj_kind_${kind}`)。

- [ ] **Step 4: 跑,确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm exec vitest run src/components/console src/api`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console apps/admin-ui/src/i18n
git commit -m "feat(console): 账本层接 SYSTEM 行(泳道 / 折叠相同提示词 / 上下文行谓词)与每步 firstTokenAt / ttft 比例"
```

---

### Task 10: Schema tab(前端:API + 懒加载 hook + 面板 + 接线)

**Files:**
- Modify: `apps/admin-ui/src/api/agents.ts:58-93`(类型 + `getAgentTools`)
- Create: `apps/admin-ui/src/components/console/useAgentTools.ts`
- Create: `apps/admin-ui/src/components/console/RowDetailSchema.tsx`
- Modify: `apps/admin-ui/src/components/console/RowDetailPayloadResult.tsx:106`(`function JsonBlock` → `export function JsonBlock`)
- Modify: `apps/admin-ui/src/components/console/RecordDetails.tsx:44-55`(`RecordTab` 加 `"schema"`、`recordTabsOf`)、`:66-90`(props 加 `toolSchemas`)、tab 分发处加 `case "schema"`
- Modify: `apps/admin-ui/src/components/console/TrajectoryView.tsx:44-58`(props 加 `agentName` / `agentVersion`)、`:160-175`(传 `toolSchemas`)
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx:658-673`(传 `agentName={r.name} agentVersion={r.version}`)
- Modify: i18n 三处:`console.detail_tab_schema: "Schema"`、`console.detail_schema_loading`(en "Loading tool schemas…" / zh "正在加载工具 schema…")、`console.detail_schema_error`("Tool schemas unavailable" / "工具 schema 加载失败")、`console.detail_schema_retry`("Retry" / "重试")、`console.detail_schema_missing`("This tool is not in the agent's current tool set" / "当前 Agent 的工具集里没有这个工具")、`console.detail_schema_source`("Source" / "来源")、`console.detail_schema_deferred`("deferred — promoted on demand" / "延迟挂载, 按需提升")、`console.detail_schema_parameters`("Parameters (JSON Schema)" / "参数 (JSON Schema)")
- Test: `apps/admin-ui/src/components/console/__tests__/useAgentTools.test.ts`(新)、`RecordDetails.test.tsx`(追加)、`TrajectoryView.test.tsx`(追加一条 props 接线)

**Interfaces:**
- Consumes: T7 端点契约(测试用 `vi.spyOn(agentsSdk, "getAgentTools")` mock)。
- Produces:
  ```ts
  // api/agents.ts
  export interface AgentToolSchema { name: string; description: string; parameters: Record<string, unknown>; source: string; from_skill: string | null; deferred: boolean }
  export interface AgentToolList { items: AgentToolSchema[]; total: number }
  export async function getAgentTools(name: string, version: string, tenantScope?: TenantScope): Promise<AgentToolList>
  // components/console/useAgentTools.ts
  export type ToolSchemaStatus = "idle" | "loading" | "ready" | "error";
  export interface ToolSchemaState { status: ToolSchemaStatus; byName: ReadonlyMap<string, AgentToolSchema>; reload: () => void }
  export function useAgentTools(args: { agentName: string; agentVersion: string; enabled: boolean }): ToolSchemaState
  // components/console/RowDetailSchema.tsx
  export function schemaToolNameOf(row: TrajectoryRow): string | null   // tool → entry.toolName;plan(update_plan) → "update_plan";其余 null
  export function SchemaPanel(props: { toolName: string; state: ToolSchemaState }): JSX.Element
  ```
  `RecordTab` 加 `"schema"`;`recordTabsOf`:`schemaToolNameOf(record.row) !== null` 的记录 = `["summary","payload","result","schema","timing","raw"]`;`RecordDetailsProps.toolSchemas: ToolSchemaState`;`TrajectoryViewProps.agentName: string; agentVersion: string`。

- [ ] **Step 1: 写失败测试**

`useAgentTools.test.ts`:

```ts
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as agentsSdk from "../../../api/agents";
import { useAgentTools } from "../useAgentTools";

const ITEM = { name: "bash", description: "run", parameters: { type: "object" }, source: "builtin", from_skill: null, deferred: false };

describe("useAgentTools", () => {
  it("stays idle until enabled, then fetches once and indexes by name", async () => {
    const spy = vi.spyOn(agentsSdk, "getAgentTools").mockResolvedValue({ items: [ITEM], total: 1 });
    const { result, rerender } = renderHook(({ enabled }) => useAgentTools({ agentName: "a", agentVersion: "1", enabled }), { initialProps: { enabled: false } });
    expect(result.current.status).toBe("idle");
    expect(spy).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.byName.get("bash")).toEqual(ITEM);
    rerender({ enabled: false }); rerender({ enabled: true });
    expect(spy).toHaveBeenCalledTimes(1); // 整个会话复用
  });
  it("error → status error; reload() refetches", async () => {
    const spy = vi.spyOn(agentsSdk, "getAgentTools").mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce({ items: [], total: 0 });
    const { result } = renderHook(() => useAgentTools({ agentName: "a", agentVersion: "1", enabled: true }));
    await waitFor(() => expect(result.current.status).toBe("error"));
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(spy).toHaveBeenCalledTimes(2);
  });
  it("agent identity change resets and refetches", async () => {
    const spy = vi.spyOn(agentsSdk, "getAgentTools").mockResolvedValue({ items: [], total: 0 });
    const { result, rerender } = renderHook((p: { v: string }) => useAgentTools({ agentName: "a", agentVersion: p.v, enabled: true }), { initialProps: { v: "1" } });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    rerender({ v: "2" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
  });
});
```

`RecordDetails.test.tsx` 追加(照文件里 `renderRecord` helper,新 prop `toolSchemas` 默认 `{ status: "idle", byName: new Map(), reload: vi.fn() }`):

```ts
describe("Schema tab (PR-A.3 §十.2)", () => {
  const ready = (items: AgentToolSchema[]) => ({ status: "ready" as const, byName: new Map(items.map((i) => [i.name, i])), reload: vi.fn() });
  it("tool record gets a Schema tab between 结果 and 计时; user/assistant do not", () => {
    const { } = renderRecord({ record: rec(toolRow("bash")), toolSchemas: ready([]) });
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["概要", "载荷", "结果", "Schema", "计时", "原始"]);
    renderRecord({ record: rec(assistantRow()), toolSchemas: ready([]) });
    expect(screen.queryByTestId("console-detail-tab-schema")).toBeNull();
  });
  it("renders description / source / deferred / JSON schema for the named tool", async () => {
    const item = { name: "bash", description: "Run a shell command", parameters: { type: "object", properties: { command: { type: "string" } } }, source: "mcp:gh", from_skill: null, deferred: true };
    renderRecord({ record: rec(toolRow("bash")), toolSchemas: ready([item]) });
    await userEvent.click(screen.getByTestId("console-detail-tab-schema"));
    const panel = screen.getByTestId("console-detail-schema");
    expect(panel).toHaveTextContent("Run a shell command");
    expect(panel).toHaveTextContent("mcp:gh");
    expect(panel).toHaveTextContent("延迟挂载");
    expect(panel).toHaveTextContent('"command"');
  });
  it("three states: loading / error with retry / missing name", async () => {
    const reload = vi.fn();
    const { rerender } = renderRecord({ record: rec(toolRow("bash")), toolSchemas: { status: "loading", byName: new Map(), reload } });
    await userEvent.click(screen.getByTestId("console-detail-tab-schema"));
    expect(screen.getByTestId("console-detail-schema")).toHaveTextContent("正在加载");
    rerender({ toolSchemas: { status: "error", byName: new Map(), reload } });
    await userEvent.click(screen.getByTestId("console-detail-schema-retry"));
    expect(reload).toHaveBeenCalledTimes(1);
    rerender({ toolSchemas: ready([]) });
    expect(screen.getByTestId("console-detail-schema-missing")).toBeInTheDocument();
  });
  it("plan(update_plan) record gets Schema for 'update_plan'; planner-node plan does not", () => {
    expect(schemaToolNameOf(planRow({ source: "update_plan" }))).toBe("update_plan");
    expect(schemaToolNameOf(planRow({ source: "planner" }))).toBeNull();
  });
});
```
(`renderRecord` 若不支持 `rerender`,拆成三次 render。)

`TrajectoryView.test.tsx` 追加:渲染时传 `agentName="a" agentVersion="1"`,选中一条 tool 记录并点 Schema tab → `getAgentTools` 被调一次(mock);选中 assistant 记录时不调。

- [ ] **Step 2: 跑,确认红**

Run: `cd apps/admin-ui && pnpm exec vitest run src/components/console/__tests__/useAgentTools.test.ts src/components/console/__tests__/RecordDetails.test.tsx src/components/console/__tests__/TrajectoryView.test.tsx`

- [ ] **Step 3: 实现**

`api/agents.ts`:照 `getAgent` 写 `getAgentTools`(路径 `/v1/agents/${name}/${version}/tools`,`getJson<AgentToolList>`)。

`useAgentTools.ts`(≤ 80 行):`useState<{status, byName}>`,`useEffect` 依赖 `[agentName, agentVersion, enabled, nonce]`:identity 变 → 复位 idle;`enabled && status === "idle"` → 置 loading 并 `getAgentTools(name, version, concreteTenantScope(useTenantScope().apiTenantScope))`(照 `useRunTrace.ts:60` 的 tenant scope 取法),成功 → ready + Map,失败 → error(`console.warn` 一次);`reload = () => setNonce(n => n+1)` 并回 idle。取消守卫:effect 里 `let cancelled = false`。

`RowDetailSchema.tsx`(≤ 120 行):`schemaToolNameOf`;`SchemaPanel`:按 state.status 渲染 —— `idle | loading` → `<Typography.Text type="secondary">{t("console.detail_schema_loading")}</Typography.Text>`;`error` → 文案 + `<Button size="small" data-testid="console-detail-schema-retry" onClick={state.reload}>{t("console.detail_schema_retry")}</Button>`;`ready` 且 `!byName.has(toolName)` → `<div data-testid="console-detail-schema-missing">…</div>`;否则 `<dl className="ew-detail__ov">` 三行(描述 / 来源 `source` + `from_skill` 的 `skill:<name>` / 延迟挂载标记)+ `<h4>{t("console.detail_schema_parameters")}</h4>` + `<JsonBlock value={item.parameters} copyTestId="console-detail-schema-copy" />`。外层 `data-testid="console-detail-schema"`。

`RecordDetails.tsx`:`RecordTab` 加 `"schema"`;`TOOLISH_TABS: RecordTab[] = ["summary","payload","result","schema","timing","raw"]`,`recordTabsOf` 先判 `schemaToolNameOf(record.row) !== null`;props 加 `toolSchemas: ToolSchemaState`;tab 分发 `case "schema": { const name = schemaToolNameOf(row); return name === null ? null : <SchemaPanel toolName={name} state={props.toolSchemas} />; }`。i18n `console.detail_tab_schema`。

`TrajectoryView.tsx`:props 加 `agentName: string; agentVersion: string;`;`const toolSchemas = useAgentTools({ agentName, agentVersion, enabled: selectedRecord !== null && schemaToolNameOf(selectedRecord.row) !== null });`(一旦 ready 就不再回 idle —— hook 内部用 status 守,不靠 enabled 变化);传给 `<RecordDetails toolSchemas={toolSchemas} />`。

`PlaygroundTab.tsx`:`<TrajectoryView agentName={r.name} agentVersion={r.version} …/>`(`r` 即 `detail.record`,line 142)。

- [ ] **Step 4: 跑,确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm exec vitest run src/components/console src/api src/pages/__tests__/PlaygroundTab.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/agents.ts apps/admin-ui/src/components/console apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx apps/admin-ui/src/i18n
git commit -m "feat(console): 详情 Schema tab —— 懒加载 GET /v1/agents/{name}/{version}/tools,工具 / update_plan 记录展示 JSON Schema"
```

---

### Task 11: 模型块 TTFT / Decoding 双色 + 提示行 + 请求详情「首 token」

**Files:**
- Modify: `apps/admin-ui/src/components/console/TrajectoryTimelineBlocks.tsx:48-67`(块加 `data-ttft` + `--traj-span-ttft`)
- Modify: `apps/admin-ui/src/components/console/trajectory_timeline.css:151-158`(assistant 双色规则,放在 `[data-error]` 之前)
- Modify: `apps/admin-ui/src/components/console/trajectory_timeline_pointer.ts:132-151`(`tooltipLines` 加首 token 行)
- Modify: `apps/admin-ui/src/components/console/RequestDetails.tsx:84-87`(「时长」之后加「首 token」)
- Modify: i18n 三处:`console.timeline_tip_ttft`("first token {{d}}" / "首 token {{d}}")、`console.detail_first_token`("First token" / "首 token")
- Test: `TrajectoryTimeline.test.tsx`(或 `TrajectoryTimelineBlocks.test.tsx`,以目录为准)、`trajectory_timeline_pointer.test.ts`、`RequestDetails.test.tsx`(都追加)

**Interfaces:**
- Consumes: `TimelineSpan.ttft`、`LedgerRecord.firstTokenAt`、`LedgerRequest.firstTokenMs`(T9)。

- [ ] **Step 1: 写失败测试**

时间轴块测试:渲染一个 `ttft: 0.25` 的 assistant span → 该 `console-lane-block` 有 `data-ttft="true"` 且 `style` 含 `--traj-span-ttft: 25%`;`ttft: null` 的块 / tool 块无 `data-ttft`;`isError` 的 assistant 块即使有 ttft 也无 `data-ttft`(失败块整块红)。

`trajectory_timeline_pointer.test.ts`:`tooltipLines` 对 `firstTokenAt = startedAt + 1200` 的 assistant 记录多一行 `首 token 1.2s`;`firstTokenAt: null` 不出这行。

`RequestDetails.test.tsx`:`firstTokenMs: 640` → 概要有「首 token 640ms」行;`null` → 「—」。

- [ ] **Step 2: 跑,确认红**

Run: `cd apps/admin-ui && pnpm exec vitest run src/components/console/__tests__/TrajectoryTimeline* src/components/console/__tests__/trajectory_timeline_pointer.test.ts src/components/console/__tests__/RequestDetails.test.tsx`

- [ ] **Step 3: 实现**

`TrajectoryTimelineBlocks.tsx` 的 `<span className="ew-traj-tl__block" …>` 加:
```tsx
data-ttft={span.kind === "assistant" && !span.isError && span.ttft !== null ? "true" : undefined}
```
style 加 `"--traj-span-ttft": span.ttft === null ? undefined : `${span.ttft * 100}%`,`。

`trajectory_timeline.css`(§十.3:前段同色 40%,后段实色):
```css
/* §十.3 模型块 TTFT / Decoding 双色:首 token 前同色 40%,之后实色;失败块不分段(仍整块 danger)。 */
.ew-traj-tl__block[data-kind="assistant"][data-ttft="true"] {
  background: linear-gradient(
    to right,
    color-mix(in srgb, var(--ew-accent-violet) 40%, transparent) 0 var(--traj-span-ttft),
    var(--ew-accent-violet) var(--traj-span-ttft) 100%
  );
}
```
(放在 `[data-error="true"]` 规则**之前**,让 error 覆盖。)

`tooltipLines`:在「总时长」行之后:
```ts
  const firstTokenAt = record?.firstTokenAt ?? null;
  if (startedAt !== null && firstTokenAt !== null && firstTokenAt >= startedAt) {
    lines.push(t("console.timeline_tip_ttft", { d: fmtDuration(Math.round(firstTokenAt - startedAt)) }));
  }
```

`RequestDetails.tsx`:「时长」行后加
```tsx
<DetailRow label={t("console.detail_first_token")}>
  {request.firstTokenMs === null ? dash : fmtDuration(request.firstTokenMs)}
</DetailRow>
```

- [ ] **Step 4: 跑,确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm exec vitest run src/components/console`

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console apps/admin-ui/src/i18n
git commit -m "feat(console): 模型块 TTFT / Decoding 双色 + 悬停首 token 行 + 请求详情「首 token」"
```

---

### Task 12: SYSTEM 行 UI(详情 tab / 原文面板 / 颜色)

**Files:**
- Create: `apps/admin-ui/src/components/console/RowDetailSystem.tsx`
- Modify: `apps/admin-ui/src/components/console/RecordDetails.tsx:46-60`(`SYSTEM_TABS` / `recordTabsOf` / `sectionsOf`)、概要 `dl`(字数行)、tab 分发 `rawtext` 分支
- Modify: `apps/admin-ui/src/components/console/kind_tag.css:29-35`(`.ew-kt--system { --ew-kt-color: var(--ew-text-secondary); }` —— 显式写出来,别靠默认)
- Modify: `apps/admin-ui/src/components/console/trajectory_timeline.css:151`(`.ew-traj-tl__block[data-kind="system"] { background: var(--ew-text-secondary); }` 同理显式)
- Modify: i18n 三处:`console.detail_system_prompt`("System prompt" / "系统提示词")、`console.detail_system_chars`("{{n}} chars" / "{{n}} 字")
- Test: `RecordDetails.test.tsx`、`TrajectoryLedgerRow.test.tsx`(或 `TrajectoryLedger.test.tsx`)、`kind_label.test.ts`(都追加)

**Interfaces:**
- Consumes: `SystemRow`(T5)、账本 `system` 记录(T9)。
- Produces: `RecordDetails` 对 `system` 记录的 tabs = `["summary","rawtext","raw"]`;`SystemPromptPanel({ text })`(`data-testid="console-detail-system-prompt"`,`<pre>` + 复制按钮 `console-detail-system-copy`)。

- [ ] **Step 1: 写失败测试**

`RecordDetails.test.tsx` 追加:

```ts
describe("SYSTEM record (PR-A.3 §十.1)", () => {
  it("tabs 概要 / 原文 / 原始; summary shows char count; 原文 shows the full prompt", async () => {
    renderRecord({ record: rec(systemRow("你是评审员\n只说重点")) });
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["概要", "原文", "原始"]);
    expect(screen.getByTestId("console-detail-summary")).toHaveTextContent("9 字");
    await userEvent.click(screen.getByTestId("console-detail-tab-rawtext"));
    expect(screen.getByTestId("console-detail-system-prompt")).toHaveTextContent("只说重点");
  });
});
```
(`systemRow(text)` fixture:`{ id: "system", kind: "system", seq: -1, step: null, status: "ok", durationMs: null, eventIndexes: [1], serverMs: 1001, text }`。)

账本行测试:`system` 记录渲染 `console-traj-kind` 文本 `SYSTEM`、内容列 = 首行、`data-kind="system"`。`kind_label.test.ts`:`kindLabel("system", t) === "SYSTEM"`。

- [ ] **Step 2: 跑,确认红**

- [ ] **Step 3: 实现**

`RowDetailSystem.tsx`(≤ 60 行):`SystemPromptPanel({ text })` → 外层 `div[data-testid=console-detail-system-prompt]`,右上 `CopyButton`(`../CopyButton`,`testId="console-detail-system-copy"`),内 `<pre>` 样式照 `RowDetailPayloadResult.tsx:89-103` 的 `Pre`(复制那几行内联样式即可,别 import 私有函数)。

`RecordDetails.tsx`:`const SYSTEM_TABS: RecordTab[] = ["summary", "rawtext", "raw"];`,`recordTabsOf` 加 `if (record.kind === "system") return SYSTEM_TABS;`;`sectionsOf` 加 `if (record.kind === "system") return ["rawtext"];`;概要 `dl`:`record.row.kind === "system"` 时多一行 `<DetailRow label={t("console.detail_system_prompt")}>{t("console.detail_system_chars", { n: row.text.length })}</DetailRow>`;`case "rawtext"`:`textRow !== null ? <AssistantRawText …/> : row.kind === "system" ? <SystemPromptPanel text={row.text} /> : null`。

CSS 两处显式规则;i18n 两键。

- [ ] **Step 4: 跑,确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm exec vitest run src/components/console && pnpm build && pnpm build-storybook && pnpm exec playwright test e2e/playground-upload.spec.ts e2e/session-history.spec.ts`

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/console apps/admin-ui/src/i18n
git commit -m "feat(console): SYSTEM 行详情(概要字数 / 原文面板)与显式配色"
```

---

### Task 13: 发布测试环境 + 真栈冒烟(PR 合并后)

**Files:**
- Modify: `infra/k8s/overlays/test/kustomization.yaml`(`release.sh` 改 newTag;kustomize 挪走的注释挪回)
- 记录 PR 正文(冒烟结果)

- [ ] **Step 1:** PR 合并后在 main 上 `tools/deploy/release.sh test`,等 `SMOKE PASS`。
- [ ] **Step 2:** 用户 `playwright codegen --save-storage` 登一次(文件只放 scratchpad,用完删);在 PR-A.2 的 `smoke-a2.mjs` 基础上加断言:
  - 新 run 的轨迹第一行是 `SYSTEM`(`console-traj-kind` = SYSTEM,泳道 0),详情「原文」有提示词全文;第二轮同提示词不再出现 SYSTEM 行;
  - 点 TOOL 行 → 详情有 `Schema` tab,面板里有 `"command"` 与来源 `builtin`;
  - 时间轴 ASSISTANT 块 `data-ttft="true"` 且 `--traj-span-ttft` 在 0–100% 之间;悬停提示含「首 token」;请求详情含「首 token」;
  - 计时 tab「Langfuse 精确」时长与「SSE 时戳」同量级(不再 ×1000),第 1 步 ASSISTANT 配到的 span 的 tokens 与请求详情用量一致;
  - 对外平面:用一把临时 API key(pod 内 mint,用完吊销,key 不出集群)`POST /v1/agents/{code}/runs` 的 SSE 正文 `grep -c "event: system_prompt"` = 0;同一 run 的控制台回放 = 1。
- [ ] **Step 3:** 开记录 PR(`chore(deploy): test overlay newTag → <sha>(PR-A.3 轨迹补数据上线)`),冒烟结果写进正文;用户合并。

---

## Self-Review

- **Spec coverage(§十)**:10.1 数据 = T2;平面 = T8;账本 / 折叠 / 计数 = T5 + T9;详情 = T12。10.2 数据 = T1 + T7(含 mcp 对齐);UI = T10。10.3 数据 = T3;UI = T11;不做项未做。10.4 = T4 + T6。10.5 类型表 = T9 / T12 的 CSS 与 tabs。发布 = T13。
- **Placeholder scan**:T7 第三条测试(API-key 请求头)与 T8 回放测试搭建两处写的是「照同文件既有测试抄」,因为那两份 helper 在各自测试文件里已经存在、不必在计划里复制一遍;其余步骤有代码。
- **Type consistency**:`ToolCatalogEntry` 字段名 ↔ 端点 `AgentToolItem` ↔ 前端 `AgentToolSchema`(`name/description/parameters/source/from_skill/deferred`)三处一致;`SystemRow.text` ↔ `contentOf` ↔ `SystemPromptPanel({text})`;`AgentStep.firstTokenMs` → `AssistantRow.firstTokenMs` → `LedgerRecord.firstTokenAt` / `LedgerRequest.firstTokenMs` → `TimelineSpan.ttft`;`CONTEXT_KINDS` 四处引用同名;`hide_events` / `EXTERNAL_HIDDEN_EVENTS` / `SYSTEM_PROMPT_EVENT` 三名一致。
